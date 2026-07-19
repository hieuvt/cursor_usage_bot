# Cursor Daily Usage Reporter

Báo cáo mức dùng Cursor (tài khoản cá nhân) của **ngày hôm trước** qua Telegram group. Chạy bằng cron trên server.

> **Cảnh báo:** Tool dùng endpoint dashboard Cursor **không chính thức**. API có thể đổi hoặc ngừng hoạt động bất kỳ lúc nào. Session cookie hết hạn thì phải cập nhật thủ công trong `config.yaml`. Khi cookie hết hạn, API đổi shape, hoặc không kết nối được Cursor, tool gửi **cảnh báo vào Telegram group** (xem SPEC §6.1).

## Tài liệu

- [docs/SPEC.md](docs/SPEC.md) — yêu cầu, API, config, ràng buộc

## Yêu cầu

- Python 3.11+
- Tài khoản Cursor cá nhân + cookie session
- Telegram bot + group chat id

## Cài đặt

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp config.example.yaml config.yaml
# điền session_token, bot_token, chat_id vào config.yaml
```

## Chạy

```bash
# Linux/macOS
export PYTHONPATH=src
python -m cursor_usage --config config.yaml --dry-run   # in stdout, không gửi Telegram
python -m cursor_usage --config config.yaml             # gửi vào group

# Windows PowerShell
$env:PYTHONPATH = "src"
python -m cursor_usage --config config.yaml --dry-run
python -m cursor_usage --config config.yaml
```

Exit codes: `0` OK · `1` lỗi chung · `2` auth (cookie hết hạn / không hợp lệ).

Hoặc dùng wrapper (Linux server):

```bash
./scripts/run_daily.sh
```

## Cron (server)

Gợi ý chạy 08:00 mỗi ngày (nên đặt timezone server = `Asia/Ho_Chi_Minh`):

```cron
0 8 * * * /opt/cursorUsage/scripts/run_daily.sh >> /var/log/cursor-usage.log 2>&1
```

Đảm bảo:

1. Repo nằm tại path trong crontab (ví dụ `/opt/cursorUsage`)
2. Có `.venv` đã `pip install -r requirements.txt`, hoặc `python` hệ thống đủ dependency
3. `config.yaml` tồn tại với secret thật
4. `chmod +x scripts/run_daily.sh`

## Lấy `WorkosCursorSessionToken`

1. Mở trình duyệt, đăng nhập [Cursor Usage Dashboard](https://cursor.com/dashboard/usage).
2. Mở DevTools (F12) → **Application** (Chrome) / **Storage** (Firefox).
3. Cookies → `https://cursor.com`.
4. Copy giá trị cookie **`WorkosCursorSessionToken`**.
5. Dán vào `config.yaml`:

```yaml
cursor:
  session_token: "<giá trị vừa copy>"
```

Cookie là secret — không commit, không chia sẻ công khai.

## Lấy Telegram `chat_id` (group)

1. Tạo bot với [@BotFather](https://t.me/BotFather) → lấy `bot_token`.
2. Add bot vào group; gửi một tin bất kỳ trong group.
3. Gọi `https://api.telegram.org/bot<TOKEN>/getUpdates` và lấy `chat.id` (group thường là số âm).
4. Điền vào `config.yaml` → `telegram.chat_id`.

## Config

Một file `config.yaml` (xem `config.example.yaml`):

| Khối | Field | Mô tả |
|------|--------|--------|
| `cursor` | `session_token` | Cookie `WorkosCursorSessionToken` |
| `cursor` | `timezone` | Mặc định `Asia/Ho_Chi_Minh` |
| `telegram` | `bot_token` | Token từ BotFather |
| `telegram` | `chat_id` | ID group (thường số âm) |
| `report` | `locale` | Mặc định `vi` |

## Vận hành — refresh cookie

Khi nhận alert `⚠️ Cursor Usage — CẢNH BÁO` về cookie/session:

1. Lấy lại `WorkosCursorSessionToken` như trên
2. Cập nhật `config.yaml`
3. Chạy lại: `python -m cursor_usage --config config.yaml --dry-run` rồi bỏ `--dry-run`

## License

Private / use at your own risk.
