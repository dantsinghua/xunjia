"""Independent DOM geometry gates for quote columns and channel labels."""
import json

from playwright.sync_api import expect


def open_quote(ui, width=1920):
    page, base, *_ = ui
    page.set_viewport_size({'width': width, 'height': 1080})
    page.goto(base + '/#/quote')
    expect(page.locator('.mkt-row').first).to_be_visible()
    return page


def test_quote_14_columns_headers_align_and_right_quotes_are_visible_at_1920(ui):
    page = open_quote(ui)
    geometry = page.evaluate('''() => {
      const table=document.querySelector('.mkt'), rows=table.tHead.rows;
      const cells=[...table.querySelector('.mkt-row').cells];
      const rect=e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right}};
      return {columns:table.querySelectorAll('col').length, body:cells.length,
        spans:[...rows[0].cells].map(c=>c.colSpan), subcolumns:rows[1].cells.length,
        basic:rect(rows[0].cells[1]), intent:rect(rows[0].cells[2]),
        external:rect(rows[0].cells[3]), status:rect(rows[0].cells[4]),
        cells:cells.map(rect), documentWidth:document.documentElement.scrollWidth,
        bodyWidth:document.body.scrollWidth, viewport:innerWidth};
    }''')
    assert geometry['columns'] == geometry['body'] == 14
    assert geometry['spans'] == [1, 2, 4, 6, 1]
    assert geometry['subcolumns'] == 12
    for group, first, last in [('basic', 1, 2), ('intent', 3, 6), ('external', 7, 12), ('status', 13, 13)]:
        assert abs(geometry[group]['left'] - geometry['cells'][first]['left']) <= 2
        assert abs(geometry[group]['right'] - geometry['cells'][last]['right']) <= 2
    assert geometry['external']['right'] <= geometry['viewport']
    assert geometry['cells'][12]['right'] <= geometry['viewport']
    assert geometry['documentWidth'] <= 1920 and geometry['bodyWidth'] <= 1920


def test_quote_overflow_scrolls_inside_table_on_narrower_viewport(ui):
    page = open_quote(ui, width=1366)
    sizes = page.evaluate('''() => {
      const box=document.querySelector('.table-scroll');box.scrollLeft=box.scrollWidth;
      return {documentWidth:document.documentElement.scrollWidth, viewport:innerWidth,
        client:box.clientWidth, scroll:box.scrollWidth, left:box.scrollLeft,
        overflow:getComputedStyle(box).overflowX};
    }''')
    assert sizes['documentWidth'] <= sizes['viewport']
    assert sizes['scroll'] > sizes['client'] and sizes['left'] > 0
    assert sizes['overflow'] in ('auto', 'scroll')


def test_empty_mode_labels_still_display_both_channel_names(ui):
    page, base, *_ = ui
    page.set_viewport_size({'width': 1920, 'height': 1080})
    links = [{'id': 'synthetic-account:' + mode, 'mode': mode, 'mode_label': '',
              'name': '', 'account_uid': 'synthetic-account', 'workbench_id': 'synthetic-wb',
              'active': mode == 'oss', 'configured': False, 'version': 1,
              'form': {mode: {}}, 'contacts': []} for mode in ('oss', 'mail')]
    page.route(base + '/api/im/links', lambda route: route.fulfill(content_type='application/json',
               body=json.dumps({'ok': True, 'data': {'active_id': links[0]['id'], 'links': links}})))
    # 本门检验已配置通道的回退名；配置前不发现目录由独立门验证。
    page.route(base + '/api/im/mq-config', lambda route: route.fulfill(content_type='application/json',
        body=json.dumps({'ok':True,'data':{'editable':True,'configured':True,'applied':True,
            'backend':'file_gateway','base_url':'https://layout.synthetic.invalid','credentials_set':True,
            'account_uid':'synthetic-account','workbench_id':'synthetic-wb','revision':'layout-fixture','note':''}})))
    page.goto(base + '/#/link')
    expect(page.locator('.channel b')).to_have_text(['文件中转', '邮件通道'])
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
