# SPEC — Cursor Daily Usage Reporter

## 1. Mục tiêu

Tool CLI Python chạy định kỳ (cron) để:

1. Query mức dùng Cursor token của **ngày hôm trước** (theo timezone cấu hình).
2. Gửi báo cáo qua **Telegram bot** vào **group chat**.

Nội dung báo cáo bắt buộc:

| Mục | Mô tả |
|-----|--------|
| Chu kỳ thanh toán | `billingCycleStart` → `billingCycleEnd` |
| Gói thuê bao | `membershipType` (ví dụ `pro`, `pro_plus`, `ultra`) |
| Khuyến mại / bonus | Nếu có (ví dụ `individualUsage.plan.breakdown.bonus`); không có thì ghi rõ |
| Usage hôm qua | Theo model: số token, chi phí ước tính |
| Usage từ đầu chu kỳ → hết hôm qua | Theo model: số token, chi phí ước tính |

## 2. Phạm vi / ngoài phạm vi

### Trong phạm vi

- Tài khoản Cursor **cá nhân** (Pro / Pro+ / Ultra)
- Python 3.11+ CLI + cron trên server Linux
- Một file cấu hình (`config.yaml`) gồm thông tin Cursor + Telegram
- Báo cáo ngày hôm trước + cycle-to-date

### Ngoài phạm vi

- Teams / Enterprise Admin API
- UI web / dashboard riêng
- Tự refresh cookie / OAuth tự động
- Lưu lịch sử usage vào database
- Tự tính lại bảng giá model (chỉ dùng số Cursor trả về)

## 3. Nguồn dữ liệu

Dùng endpoint dashboard Cursor (**không chính thức**, có thể đổi/break).

| Endpoint | Method | Dùng để |
|----------|--------|---------|
| `https://cursor.com/api/usage-summary` | GET | Chu kỳ billing, gói (`membershipType`), plan used/limit, bonus nếu có |
| `https://cursor.com/api/dashboard/get-filtered-usage-events` | POST | Events chi tiết: model, tokens, `chargedCents` |

### Auth

```
Cookie: WorkosCursorSessionToken=<token>
```

### Header bắt buộc cho POST

```
Origin: https://cursor.com
Content-Type: application/json
```

### Body events (gợi ý)

```json
{
  "startDate": "<epoch_ms_string>",
  "endDate": "<epoch_ms_string>",
  "page": 1,
  "pageSize": 100
}
```

`startDate` / `endDate` là epoch milliseconds dạng **string**. Paginate (`page`) đến khi lấy đủ `totalUsageEventsCount`.

### Cách lấy session token

1. Đăng nhập https://cursor.com/dashboard/usage
2. DevTools (F12) → Application → Cookies → `https://cursor.com`
3. Copy giá trị cookie `WorkosCursorSessionToken`
4. Dán vào `config.yaml` → `cursor.session_token`

## 4. Config schema

Một file duy nhất (ví dụ `config.yaml`):

```yaml
cursor:
  session_token: "..."          # WorkosCursorSessionToken
  timezone: "Asia/Ho_Chi_Minh"  # để tính "hôm qua"

telegram:
  bot_token: "..."
  chat_id: "..."                # group id, thường là số âm

report:
  locale: "vi"
```

| Field | Bắt buộc | Default | Mô tả |
|-------|----------|---------|--------|
| `cursor.session_token` | Có | — | Cookie session Cursor |
| `cursor.timezone` | Không | `Asia/Ho_Chi_Minh` | Timezone tính “hôm qua” |
| `telegram.bot_token` | Có | — | Bot token từ BotFather |
| `telegram.chat_id` | Có | — | ID group chat |
| `report.locale` | Không | `vi` | Locale format báo cáo |

File mẫu không secret: `config.example.yaml`. File thật `config.yaml` **không** commit.

## 5. Format báo cáo (logic)

### Thời gian

- Timezone mặc định: `Asia/Ho_Chi_Minh` (UTC+7)
- **Yesterday**: `[00:00:00, 23:59:59.999]` của ngày hôm trước theo timezone
- **Cycle to date**: `[billingCycleStart, endOfYesterday]`
- Fetch events một lần cho cycle window, rồi filter in-memory cho yesterday

### Chi phí ước tính

- Ưu tiên: `chargedCents / 100` (USD)
- Fallback: `tokenUsage.totalCents / 100`
- Thiếu cả hai → `0`
- **Không** tự nhân với bảng giá model

### Token

Cộng dồn từ `tokenUsage` khi có:

- `inputTokens`
- `outputTokens`
- `cacheWriteTokens` (và cache read nếu API trả)

Group theo `model`; thiếu model → `"unknown"`.

### Khuyến mại / bonus

- Có `individualUsage.plan.breakdown.bonus` và `bonus > 0` → ghi nhận trong báo cáo
- Có thể kèm message từ summary nếu hữu ích
- Không có → ghi **“Không có khuyến mại”**

### Đơn vị đo & tiền trên báo cáo

**Plan unit** là đơn vị cơ bản (từ event: `chargedCents` raw ≈ 1 plan unit).

| Số trên báo cáo | Cách lấy |
|-----------------|----------|
| Số tiền included | Map `membershipType` → giá tháng (pro $20, pro_plus $60, ultra $200) |
| Số plan unit included | `plan.limit` |
| USD / 1 plan unit | `included_money / included_plan_units` |
| Plan unit bonus (pool) | `breakdown.bonus*` nếu có; không có → `0` |
| Plan unit / tiền đã dùng (chu kỳ, hôm qua) | Cộng `chargedCents` events trong cửa sổ; tiền = `units × usd_per_unit` |
| Included còn lại | `max(0, included_pool − units_used_from_included)`; tiền = còn lại × `usd_per_unit` |
| On-demand đã trả | `individualUsage.onDemand.used / 100` |
| Ước tính EOC units | `avg_daily × tổng_ngày_chu_kỳ` (avg từ units đến hết hôm qua) |
| Ước tính on-demand EOC | `max(0, projected − included − bonus) × usd_per_unit` |

Mọi số usage (chu kỳ / hôm qua / theo model) **cùng mốc hết hôm qua** từ events — không trộn `plan.used` live.

`days_elapsed` = từ `billingCycleStart` → `report_date` (inclusive).  
`remaining_days` = sau `report_date` → `billingCycleEnd`.

Các số dự báo là **ước tính**, không thay thế invoice Cursor.

### Kênh gửi

- Telegram Bot API `sendMessage` → `telegram.chat_id`
- `parse_mode=HTML`
- Tách message nếu vượt 4096 ký tự

## 6. Ràng buộc vận hành

1. Endpoint dashboard **không chính thức** — có thể đổi mà không thông báo.
2. Cookie session **hết hạn** → cập nhật `config.yaml` thủ công.
3. Không commit secret (`config.yaml`, token, cookie).
4. Chi phí chỉ lấy từ field Cursor trả về, không tự tính lại pricing.
5. Lịch chạy đề xuất (cron): mỗi ngày ~08:00, sau khi ngày hôm trước đã đóng.

```cron
0 8 * * * /path/to/cursorUsage/scripts/run_daily.sh >> /var/log/cursor-usage.log 2>&1
```

### 6.1. Cảnh báo Telegram khi ràng buộc vận hành bị kích hoạt

Khi cron chạy mà gặp sự cố vận hành (không gửi được báo cáo usage bình thường), tool **phải gửi message cảnh báo** vào cùng group Telegram (`telegram.chat_id`) để người vận hành biết đã có thay đổi / cần can thiệp.

| Tình huống | Nhận diện (gợi ý) | Nội dung alert (tối thiểu) |
|------------|-------------------|----------------------------|
| Cookie hết hạn / không hợp lệ | HTTP 401, body `not_authenticated`, `CursorAuthError` | Nêu rõ cookie/session hết hạn hoặc không hợp lệ; nhắc cập nhật `cursor.session_token` trong `config.yaml` |
| API dashboard đổi / response bất thường | HTTP 4xx/5xx không phải auth; thiếu field bắt buộc (`billingCycleStart`, `usageEventsDisplay`, …); JSON không parse được; schema lệch so với SPEC | Nêu rõ endpoint + status/lỗi; ghi “có thể API dashboard đã thay đổi”; kèm message exception ngắn |
| Lỗi mạng / Cursor không phản hồi | timeout, connection error | Nêu rõ không kết nối được tới Cursor; thử lại sau |

Quy tắc gửi alert:

- Chỉ gửi khi **không** `--dry-run` và đã load được ít nhất `telegram.bot_token` + `telegram.chat_id`.
- Alert dùng prefix rõ ràng (ví dụ `⚠️ Cursor Usage — CẢNH BÁO`) để phân biệt với báo cáo hàng ngày.
- Sau khi gửi alert (hoặc thất bại khi gửi alert), process exit code ≠ 0.
- Nếu chính Telegram cũng lỗi: ghi log stderr; không nuốt exception im lặng.

Mục tiêu: người nhận biết **đã có sự thay đổi / sự cố** ngay trong group, không phải chỉ xem log server.

## 7. Kiến trúc module (tham chiếu)

```
src/cursor_usage/
  config.py           # load/validate config
  cursor_client.py    # gọi usage-summary + filtered-usage-events
  aggregator.py       # yesterday + cycle-to-date, group by model
  formatter.py        # HTML message tiếng Việt
  telegram_sender.py  # sendMessage + split
  main.py             # CLI: fetch → aggregate → send
```

Chi tiết vận hành và format báo cáo: xem các mục trên trong SPEC này.
