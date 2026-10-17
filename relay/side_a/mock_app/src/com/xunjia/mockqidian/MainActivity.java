package com.xunjia.mockqidian;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.ListView;
import android.widget.TextView;

import java.util.List;

/** 会话列表页(READY 稳态)。 */
public class MainActivity extends Activity implements Store.Listener {
    private Store store;
    private ListView list;
    private List<String> peers;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        store = Store.get(this);
        list = findViewById(R.id.thread_list);
        peers = store.peers();
        list.setAdapter(new BaseAdapter() {
            @Override public int getCount() { return peers.size(); }
            @Override public Object getItem(int i) { return peers.get(i); }
            @Override public long getItemId(int i) { return i; }
            @Override public View getView(int i, View convert, ViewGroup parent) {
                View v = convert != null ? convert : getLayoutInflater().inflate(R.layout.row_thread, parent, false);
                String peer = peers.get(i);
                ((TextView) v.findViewById(R.id.thread_name)).setText(peer);
                List<Store.Msg> h = store.history(peer);
                String preview = h.isEmpty() ? "" : h.get(h.size() - 1).text;
                ((TextView) v.findViewById(R.id.thread_preview)).setText(preview);
                TextView badge = v.findViewById(R.id.thread_unread);
                int n = store.unread(peer);
                badge.setText(String.valueOf(n));
                badge.setVisibility(n > 0 ? View.VISIBLE : View.GONE);
                return v;
            }
        });
        list.setOnItemClickListener((p, v, i, id) -> {
            Intent it = new Intent(this, ChatActivity.class);
            it.putExtra("peer", peers.get(i));
            startActivity(it);
        });
    }

    @Override protected void onResume() {
        super.onResume();
        store.addListener(this);
        onChanged();
    }

    @Override protected void onPause() {
        super.onPause();
        store.removeListener(this);
    }

    @Override public void onChanged() {
        runOnUiThread(() -> {
            peers = store.peers();
            ((BaseAdapter) list.getAdapter()).notifyDataSetChanged();
        });
    }
}
