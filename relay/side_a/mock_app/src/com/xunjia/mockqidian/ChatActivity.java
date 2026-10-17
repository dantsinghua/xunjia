package com.xunjia.mockqidian;

import android.app.Activity;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;

import java.util.List;

/** 会话页(CHAT_OPEN 瞬态)。标题 chat_title == peer 是 RPA 发送闸门的锚点。 */
public class ChatActivity extends Activity implements Store.Listener {
    private Store store;
    private String peer;
    private ListView list;
    private List<Store.Msg> msgs;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_chat);
        store = Store.get(this);
        peer = getIntent().getStringExtra("peer");
        ((TextView) findViewById(R.id.chat_title)).setText(peer);
        list = findViewById(R.id.msg_list);
        msgs = store.history(peer);
        list.setAdapter(new BaseAdapter() {
            @Override public int getCount() { return msgs.size(); }
            @Override public Object getItem(int i) { return msgs.get(i); }
            @Override public long getItemId(int i) { return i; }
            @Override public View getView(int i, View convert, ViewGroup parent) {
                View v = convert != null ? convert : getLayoutInflater().inflate(R.layout.row_msg, parent, false);
                Store.Msg m = msgs.get(i);
                LinearLayout row = v.findViewById(R.id.msg_row);
                TextView tv = v.findViewById(R.id.msg_text);
                tv.setText(m.text);
                // 发送方通过 content-desc 暴露给 uiautomator:peer / me
                tv.setContentDescription(m.dir);
                row.setGravity("me".equals(m.dir) ? Gravity.END : Gravity.START);
                tv.setBackgroundColor("me".equals(m.dir) ? 0xFFB2F0A0 : 0xFFFFFFFF);
                return v;
            }
        });
        EditText input = findViewById(R.id.input);
        Button send = findViewById(R.id.send);
        send.setOnClickListener(v -> {
            String t = input.getText().toString().trim();
            if (t.isEmpty()) return;
            store.append(peer, "me", t);
            input.setText("");
        });
        findViewById(R.id.back).setOnClickListener(v -> finish());
    }

    @Override protected void onResume() {
        super.onResume();
        store.addListener(this);
        store.clearUnread(peer);
        onChanged();
    }

    @Override protected void onPause() {
        super.onPause();
        store.removeListener(this);
    }

    @Override public void onChanged() {
        runOnUiThread(() -> {
            msgs = store.history(peer);
            ((BaseAdapter) list.getAdapter()).notifyDataSetChanged();
            list.setSelection(msgs.size() - 1);
        });
    }
}
