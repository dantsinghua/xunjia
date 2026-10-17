-- QB(羿珩 DM)经纪商行情真实数据(浏览器旁路采集,见 agent/qb_ingest.py)
-- 与 bondtrade_test 里的合成造数表(bond_broker_quote 等)分开存放,互不污染。

-- 经纪商最优报价:每次行变化(seq_ns 变)存一行 → 天然是报价变动历史
CREATE TABLE IF NOT EXISTS qb_live_best_quote (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    capture_time    DATETIME(3)  NOT NULL COMMENT '本机收到时间',
    quote_key       VARCHAR(40)  NOT NULL COMMENT '经纪商id_bondUniCode',
    quote_uuid      VARCHAR(40)  NULL,
    seq_ns          BIGINT       NOT NULL COMMENT '行版本戳(纳秒)',
    bond_uni_code   BIGINT       NOT NULL,
    broker_id       INT          NOT NULL,
    tenor           VARCHAR(16)  NULL COMMENT '剩余期限',
    remain_days     INT          NULL,
    maturity_date   DATE         NULL,
    coupon_rate     DECIMAL(10,4) NULL,
    issue_amount    DECIMAL(14,4) NULL,
    bid_yield       DECIMAL(10,4) NULL,
    bid_quote_type  INT NULL,
    bid_str         VARCHAR(32)  NULL COMMENT '带*=请示',
    bid_pending     TINYINT      NULL,
    bid_net_price   DECIMAL(12,4) NULL,
    bid_vol         DECIMAL(14,2) NULL COMMENT '买量(万)',
    bid_vol_str     VARCHAR(255) NULL COMMENT '买量原文(含拆量/清算备注)',
    ofr_yield       DECIMAL(10,4) NULL,
    ofr_quote_type  INT NULL,
    ofr_str         VARCHAR(32)  NULL,
    ofr_pending     TINYINT      NULL,
    ofr_net_price   DECIMAL(12,4) NULL,
    ofr_vol         DECIMAL(14,2) NULL,
    ofr_vol_str     VARCHAR(255) NULL,
    cnbd_yield_ex   DECIMAL(10,4) NULL COMMENT '中债估值(行权)?',
    cnbd_yield_mat  DECIMAL(10,4) NULL COMMENT '中债估值(到期)?',
    cnbd_date       DATE NULL COMMENT '来源明确的估值日，未知留空',
    cnbd_net_price  DECIMAL(12,4) NULL,
    cnbd_duration   DECIMAL(10,4) NULL,
    csi_yield_ex    DECIMAL(10,4) NULL COMMENT '中证估值?',
    csi_yield_mat   DECIMAL(10,4) NULL,
    update_time     DATETIME(3)  NULL COMMENT '报价更新时间',
    raw_json        JSON         NULL COMMENT '原始 protobuf 通览,供重映射',
    UNIQUE KEY uk_key_seq (quote_key, seq_ns),
    KEY idx_bond_time (bond_uni_code, update_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='QB 经纪商最优报价(真实,旁路采集)';

CREATE TABLE IF NOT EXISTS qb_live_trade (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    capture_time    DATETIME(3)  NOT NULL,
    trade_key       VARCHAR(64)  NOT NULL COMMENT '经纪商id_成交id_bondUniCode',
    trade_uuid      VARCHAR(40)  NULL,
    seq_ns          BIGINT       NULL,
    bond_uni_code   BIGINT       NOT NULL,
    broker_id       INT          NOT NULL,
    tenor           VARCHAR(16)  NULL,
    remain_days     INT          NULL,
    maturity_date   DATE         NULL,
    trade_time      DATETIME(3)  NULL,
    trade_yield     DECIMAL(10,4) NULL,
    trade_net_price DECIMAL(12,4) NULL,
    chg_bp          DECIMAL(10,4) NULL,
    cnbd_yield      DECIMAL(10,4) NULL,
    vs_cnbd_bp      DECIMAL(10,4) NULL,
    deal_type       INT          NULL COMMENT '? TKN/GVN/TRD 编码',
    deal_flag10     INT          NULL,
    settle_text     VARCHAR(32)  NULL COMMENT '清算速度原文',
    day_last        DECIMAL(10,4) NULL,
    day_high        DECIMAL(10,4) NULL,
    day_low         DECIMAL(10,4) NULL,
    day_count       INT          NULL,
    raw_json        JSON         NULL,
    UNIQUE KEY uk_trade (trade_key),
    KEY idx_bond_time (bond_uni_code, trade_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='QB 今日成交(真实,旁路采集)';

-- 经纪商 id → 名称(待从页面对照补齐)
CREATE TABLE IF NOT EXISTS qb_broker (
    broker_id   INT PRIMARY KEY,
    broker_name VARCHAR(32) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
;
-- 债券字典(SharedWorker 下发,页面加载时旁路捕获)
CREATE TABLE IF NOT EXISTS qb_bond_dict (
    bond_uni_code   BIGINT PRIMARY KEY,
    bond_code       VARCHAR(32)  NULL COMMENT '如 2600002.IB',
    bond_short_name VARCHAR(64)  NULL,
    com_short_name  VARCHAR(64)  NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
;
-- 每个「经纪商×券」当前最新一行(今日),带代码/简称/经纪商名;库时区 UTC、入库时间为北京时间,故 +8h 取今日
CREATE OR REPLACE VIEW qb_live_best_quote_latest AS
SELECT b.name_broker AS broker, d.bond_code, d.bond_short_name, q.*
FROM qb_live_best_quote q
JOIN (SELECT quote_key, MAX(seq_ns) AS seq_ns FROM qb_live_best_quote
      WHERE update_time >= DATE(NOW() + INTERVAL 8 HOUR) GROUP BY quote_key) m USING (quote_key, seq_ns)
LEFT JOIN qb_bond_dict d ON d.bond_uni_code = q.bond_uni_code
LEFT JOIN (SELECT broker_id, broker_name AS name_broker FROM qb_broker) b ON b.broker_id = q.broker_id
