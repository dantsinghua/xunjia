package com.xunjia.mockqidian;

import android.content.Context;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 会话/消息内存库,落盘到 files/store.json(adb 可直接 cat 作为验证证据)。 */
public final class Store {
    public static final String TAG = "MockQidian";

    public static final class Msg {
        public final String dir; // "peer" | "me"
        public final String text;
        public final long ts;
        Msg(String dir, String text, long ts) { this.dir = dir; this.text = text; this.ts = ts; }
    }

    public interface Listener { void onChanged(); }

    private static Store INSTANCE;
    private final File file;
    private final LinkedHashMap<String, List<Msg>> threads = new LinkedHashMap<>();
    private final Map<String, Integer> unread = new LinkedHashMap<>();
    private final List<Listener> listeners = new ArrayList<>();

    public static synchronized Store get(Context ctx) {
        if (INSTANCE == null) INSTANCE = new Store(ctx.getApplicationContext());
        return INSTANCE;
    }

    private Store(Context ctx) {
        file = new File(ctx.getFilesDir(), "store.json");
        if (!load()) seed();
    }

    public synchronized void seed() {
        threads.clear();
        unread.clear();
        long t = System.currentTimeMillis();
        put("张三-华泰固收", "peer", "在的,今天看什么券", t - 600000);
        put("张三-华泰固收", "me", "bid 102400895.IB 24苏科技城MTN001 3000W 估值+5能出么", t - 590000);
        put("李四-中信自营", "peer", "早,有量随时报", t - 500000);
        put("王五-国君资管", "peer", "今天先不看了", t - 400000);
        unread.clear();
        save();
        notifyChanged();
    }

    public synchronized List<String> peers() { return new ArrayList<>(threads.keySet()); }

    public synchronized List<Msg> history(String peer) {
        List<Msg> l = threads.get(peer);
        return l == null ? new ArrayList<>() : new ArrayList<>(l);
    }

    public synchronized int unread(String peer) {
        Integer n = unread.get(peer);
        return n == null ? 0 : n;
    }

    public synchronized void clearUnread(String peer) {
        unread.put(peer, 0);
        notifyChanged();
    }

    public synchronized void append(String peer, String dir, String text) {
        put(peer, dir, text, System.currentTimeMillis());
        if ("peer".equals(dir)) unread.put(peer, unread(peer) + 1);
        Log.i(TAG, "append peer=" + peer + " dir=" + dir + " text=" + text);
        save();
        notifyChanged();
    }

    private void put(String peer, String dir, String text, long ts) {
        List<Msg> l = threads.get(peer);
        if (l == null) { l = new ArrayList<>(); threads.put(peer, l); }
        l.add(new Msg(dir, text, ts));
    }

    public synchronized void addListener(Listener l) { listeners.add(l); }
    public synchronized void removeListener(Listener l) { listeners.remove(l); }

    private void notifyChanged() {
        for (Listener l : new ArrayList<>(listeners)) l.onChanged();
    }

    private void save() {
        try {
            JSONObject root = new JSONObject();
            for (Map.Entry<String, List<Msg>> e : threads.entrySet()) {
                JSONArray arr = new JSONArray();
                for (Msg m : e.getValue()) {
                    JSONObject o = new JSONObject();
                    o.put("dir", m.dir); o.put("text", m.text); o.put("ts", m.ts);
                    arr.put(o);
                }
                root.put(e.getKey(), arr);
            }
            try (FileOutputStream fo = new FileOutputStream(file)) {
                fo.write(root.toString(2).getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception ex) {
            Log.e(TAG, "save failed", ex);
        }
    }

    private boolean load() {
        if (!file.exists()) return false;
        try (FileInputStream fi = new FileInputStream(file)) {
            byte[] buf = new byte[(int) file.length()];
            int n = fi.read(buf);
            JSONObject root = new JSONObject(new String(buf, 0, n, StandardCharsets.UTF_8));
            for (java.util.Iterator<String> it = root.keys(); it.hasNext(); ) {
                String peer = it.next();
                JSONArray arr = root.getJSONArray(peer);
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.getJSONObject(i);
                    put(peer, o.getString("dir"), o.getString("text"), o.getLong("ts"));
                }
            }
            return !threads.isEmpty();
        } catch (Exception ex) {
            Log.e(TAG, "load failed", ex);
            return false;
        }
    }
}
