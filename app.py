import os
import uuid
from datetime import datetime, date, timezone, timedelta

from flask import Flask, render_template, request, redirect, url_for, abort

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 画像は5MBまで

JST = timezone(timedelta(hours=9))
SUBJECTS = ["数学", "英語", "国語", "理科", "社会", "その他"]
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
EXAM_NAME = os.environ.get("EXAM_NAME", "共通テスト")
EXAM_DATE = os.environ.get("EXAM_DATE", "2027-01-16")

CONN_STR = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
TABLE_NAME = "posts"
CONTAINER = "images"
PARTITION = "board"

table = None
container = None
if CONN_STR:
    from azure.data.tables import TableServiceClient
    from azure.storage.blob import BlobServiceClient, ContentSettings

    table = TableServiceClient.from_connection_string(CONN_STR).create_table_if_not_exists(TABLE_NAME)
    container = BlobServiceClient.from_connection_string(CONN_STR).get_container_client(CONTAINER)

_memory = {}  # ローカル実行用（Azure未接続時）


def hero_url():
    if container is not None:
        return f"{container.url}/hero.svg"
    return url_for("static", filename="hero.svg")


def save_image(file):
    if not file or not file.filename:
        return ""
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXT:
        abort(400, "画像は png / jpg / gif / webp のみです")
    name = f"posts/{uuid.uuid4().hex}.{ext}"
    if container is not None:
        container.upload_blob(
            name, file.stream, overwrite=True,
            content_settings=ContentSettings(content_type=file.mimetype or "image/" + ext),
        )
        return f"{container.url}/{name}"
    os.makedirs(os.path.join(app.static_folder, "uploads"), exist_ok=True)
    local = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(app.static_folder, "uploads", local))
    return url_for("static", filename=f"uploads/{local}")


def list_posts():
    if table is not None:
        rows = [dict(e) for e in table.query_entities(f"PartitionKey eq '{PARTITION}'")]
    else:
        rows = list(_memory.values())
    rows.sort(key=lambda r: r.get("created", ""), reverse=True)
    return rows


def insert_post(entity):
    if table is not None:
        table.create_entity(entity)
    else:
        _memory[entity["RowKey"]] = entity


def mark_solved(row_key):
    if table is not None:
        from azure.data.tables import UpdateMode
        table.update_entity({"PartitionKey": PARTITION, "RowKey": row_key, "solved": True}, mode=UpdateMode.MERGE)
    elif row_key in _memory:
        _memory[row_key]["solved"] = True


def board_url(subject=None, open_only=False, **extra):
    return url_for("index", subject=subject or None, open="1" if open_only else None, **extra)


@app.route("/")
def index():
    subject = request.args.get("subject")
    if subject not in SUBJECTS:
        subject = None
    open_only = request.args.get("open") == "1"
    all_posts = list_posts()
    open_counts = {s: 0 for s in SUBJECTS}
    for p in all_posts:
        if not p.get("solved") and p.get("subject") in open_counts:
            open_counts[p["subject"]] += 1
    posts = [p for p in all_posts
             if (not subject or p.get("subject") == subject) and (not open_only or not p.get("solved"))]
    days_left = (date.fromisoformat(EXAM_DATE) - datetime.now(JST).date()).days
    return render_template(
        "index.html",
        posts=posts,
        subjects=SUBJECTS,
        current=subject,
        open_only=open_only,
        open_counts=open_counts,
        open_total=sum(open_counts.values()),
        solved_total=sum(1 for p in all_posts if p.get("solved")),
        exam_name=EXAM_NAME,
        exam_date=date.fromisoformat(EXAM_DATE),
        days_left=days_left,
        hero=hero_url(),
        just_solved=request.args.get("solved"),
        just_posted=request.args.get("posted"),
        board_url=board_url,
    )


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(413)
def friendly_error(e):
    messages = {
        413: "画像が大きすぎます。5MB以下の写真を選んでください。",
        404: "ページが見つかりませんでした。",
    }
    msg = messages.get(e.code) or getattr(e, "description", "") or "入力を確認してください。"
    return render_template("error.html", message=msg, code=e.code), e.code


@app.route("/post", methods=["POST"])
def post():
    title = request.form.get("title", "").strip()[:60]
    body = request.form.get("body", "").strip()[:1000]
    subject = request.form.get("subject", "その他")
    nickname = request.form.get("nickname", "").strip()[:20] or "名無しの受験生"
    if not title or not body:
        abort(400, "タイトルと本文は必須です")
    if subject not in SUBJECTS:
        subject = "その他"
    row_key = uuid.uuid4().hex
    insert_post({
        "PartitionKey": PARTITION,
        "RowKey": row_key,
        "nickname": nickname,
        "subject": subject,
        "title": title,
        "body": body,
        "image": save_image(request.files.get("image")),
        "solved": False,
        "created": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
    })
    back = request.form.get("back")
    return redirect(board_url(back if back in SUBJECTS else None, posted=row_key) + f"#p-{row_key}")


@app.route("/solve/<row_key>", methods=["POST"])
def solve(row_key):
    if not row_key.isalnum():
        abort(400)
    mark_solved(row_key)
    back = request.form.get("back")
    # 未解決フィルター中でも、解決した投稿の花丸が見えるようにフィルターは外して戻す
    return redirect(board_url(back if back in SUBJECTS else None, solved=row_key) + f"#p-{row_key}")


if __name__ == "__main__":
    app.run(debug=True)
