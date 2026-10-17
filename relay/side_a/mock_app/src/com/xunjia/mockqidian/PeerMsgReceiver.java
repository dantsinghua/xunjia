package com.xunjia.mockqidian;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 测试钩子:模拟"对方发来一条消息"。仅 mock 目标有,真实企点无此通道。 */
public class PeerMsgReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        String action = intent.getAction();
        if ("com.xunjia.mockqidian.RESET".equals(action)) {
            Store.get(ctx).seed();
            return;
        }
        String peer = intent.getStringExtra("peer");
        String text = intent.getStringExtra("text");
        if (peer == null || text == null) return;
        Store.get(ctx).append(peer, "peer", text);
    }
}
