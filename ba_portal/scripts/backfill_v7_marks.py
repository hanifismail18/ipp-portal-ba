# v7 backfill: add issue_marks (one box call per BA with locatable problems) to live rows; retries 429 later
import sys, os, json, sqlite3, time
sys.path.insert(0, "/home/han/Downloads/IPP/_work/ba_portal"); os.chdir("/home/han/Downloads/IPP/_work/ba_portal")
import server, preprocess
db = sqlite3.connect(os.environ["LIVE_DB"])
todo = [r for r in db.execute("select id, filename, extraction_json, path from ba where status='done'")]
for rnd in range(int(os.environ.get("ROUNDS", "6"))):
    left = []
    for i, fn, e, p in todo:
        ex = json.loads(e)
        if ex.get("issue_marks") is not None:
            continue
        calls = []
        ex = server.locate_sig(ex, p, lambda **kw: calls.append(kw) or server.DB.log_usage(ba_id=i, **kw))
        if ex.get("issue_marks") is None:
            left.append((i, fn, e, p)); print("retry later", fn[:40], flush=True); continue
        res = server.build_result(ex, preprocess.prepare(p, server.PAGE_CAP))
        db.execute("update ba set extraction_json=?, result_json=? where id=?",
                   (json.dumps(ex, ensure_ascii=False), json.dumps(res, ensure_ascii=False), i)); db.commit()
        print("ok", fn[:40], len(ex["issue_marks"]), [c["model"] for c in calls if c["status"] == "ok"], flush=True)
    todo = left
    if not todo:
        break
    time.sleep(70)
print("left", [t[1] for t in todo])
