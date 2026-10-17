"""好友并进通讯录。不连采集端。"""
from imlink.friends import merge_people, private_friends


def test_merge_keeps_org_people_and_adds_friend_once():
    current = [{"uin": "3000000001", "name": "机构甲", "remark": ""}]
    extra = [{"uin": "415011447", "name": "合成好友", "remark": ""}, {"uin": "3000000001", "name": "重复", "remark": ""}]
    merged = merge_people(current, extra)
    assert [item["uin"] for item in merged] == ["3000000001", "415011447"]
    assert merged[1]["name"] == "合成好友"


def test_private_friends_skip_without_agent_url(monkeypatch):
    monkeypatch.delenv("QTRADE_AGENT_URL", raising=False)
    assert private_friends("3007378246") == []
