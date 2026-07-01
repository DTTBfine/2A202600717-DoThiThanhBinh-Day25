# Báo Cáo Lab Ngày 10 — Reliability Engineering

## 1. Kiến trúc hệ thống

```
Yêu cầu từ người dùng
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│                     ReliabilityGateway                      │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Tầng Cache (ResponseCache / SharedRedisCache)       │   │
│  │  • Tính độ tương đồng n-gram cosine                  │   │
│  │  • Chặn truy vấn nhạy cảm (privacy guard)            │   │
│  │  • Phát hiện false-hit theo số 4 chữ số              │   │
│  └──────────────────┬─────────────────────────────────┘   │
│                     │ MISS                                  │
│                     ▼                                       │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  CircuitBreaker: primary (fail_rate 25%)             │   │
│  │  CLOSED ──(≥3 lỗi)──▶ OPEN ──(sau 2 giây)──▶        │   │
│  │                               HALF_OPEN              │   │
│  │  HALF_OPEN ──(probe thành công)──▶ CLOSED            │   │
│  └──────────────────┬─────────────────────────────────┘   │
│                     │ OPEN hoặc ProviderError               │
│                     ▼                                       │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  CircuitBreaker: backup (fail_rate 5%)               │   │
│  └──────────────────┬─────────────────────────────────┘   │
│                     │ Tất cả provider đều thất bại          │
│                     ▼                                       │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Static Fallback — thông báo hệ thống tạm gián đoạn  │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
  JSON trace → reports/trace.jsonl
```

### Lý do thiết kế theo thứ tự: Cache → CircuitBreaker → Fallback

Thứ tự này không phải tuỳ tiện mà có chủ đích:

- **Cache đặt trước circuit breaker** vì mục tiêu đầu tiên là tránh gọi provider hoàn toàn. Mỗi lần cache hit tiết kiệm được thời gian provider (200–320 ms) và chi phí API. Nếu đặt circuit breaker trước, hệ thống sẽ cố gọi provider dù câu trả lời đã có sẵn.

- **Circuit breaker bọc từng provider riêng lẻ** thay vì bọc toàn bộ fallback chain. Nhờ vậy, khi primary đang OPEN, backup vẫn tiếp tục hoạt động bình thường — không bị kéo theo trạng thái lỗi của primary. Nếu dùng một circuit breaker chung, một provider lỗi sẽ chặn toàn bộ hệ thống.

- **Static fallback là lưới an toàn cuối cùng** thay vì để exception nổi lên client. Điều này đảm bảo hệ thống luôn trả về một phản hồi có nghĩa, dù trong trường hợp tồi tệ nhất.

---

## 2. Circuit Breaker — Lý do lựa chọn thiết kế

### Tại sao dùng 3 trạng thái (CLOSED / OPEN / HALF_OPEN)?

Mô hình 2 trạng thái (chỉ OPEN/CLOSED) có nhược điểm: sau timeout, circuit tự đóng mà không kiểm chứng xem provider đã hồi phục chưa. Nếu provider vẫn còn lỗi, hệ thống sẽ mở circuit ngay lập tức và tạo ra vòng lặp mở-đóng liên tục (flapping), gây tải không đều.

Trạng thái `HALF_OPEN` giải quyết bằng cách chỉ cho một "probe request" đi qua. Nếu thành công → đóng circuit và khôi phục hoàn toàn. Nếu thất bại → mở lại ngay với lý do `probe_failure` (phân biệt với `failure_threshold_reached`). Việc phân biệt lý do quan trọng cho observability: nhìn vào log biết ngay đây là lỗi phục hồi hay lỗi ban đầu.

### Tại sao `failure_threshold = 3`?

Chọn 3 thay vì 1 vì provider thực tế có tỷ lệ lỗi ngẫu nhiên (`fail_rate: 0.25`). Với threshold = 1, một lỗi đơn lẻ sẽ mở circuit và bắt đầu chuyển hướng toàn bộ traffic sang backup — gây overhead không cần thiết. Với threshold = 3, hệ thống chịu được các burst lỗi ngắn mà không mất tính ổn định.

### Tại sao `reset_timeout_seconds = 2`?

Provider giả lập có `base_latency_ms = 180`. Hai giây (≈ 11 lần latency P50) đủ để provider "thở" sau một đợt lỗi, nhưng không quá dài để traffic bị giữ ở backup lâu hơn cần thiết. Kết quả thực tế: `recovery_time_ms ≈ 2 466 ms` — hợp lý với ngưỡng SLO < 5 000 ms.

### Tại sao `if/elif` thay vì `if/or` trong `record_failure()`?

```python
# SAI — cùng reason cho hai trường hợp khác nhau
if self.state == CircuitState.HALF_OPEN or self.failure_count >= self.failure_threshold:
    self._transition(CircuitState.OPEN, "threshold_reached")  # mất thông tin

# ĐÚNG — phân biệt rõ nguyên nhân để trace được
if self.state == CircuitState.HALF_OPEN:
    self._transition(CircuitState.OPEN, "probe_failure")      # probe thất bại
elif self.failure_count >= self.failure_threshold:
    self._transition(CircuitState.OPEN, "failure_threshold_reached")  # lỗi tích lũy
```

Khi đọc `trace.jsonl`, `reason: "probe_failure"` cho biết hệ thống đang trong giai đoạn phục hồi nhưng provider vẫn chưa ổn định — khác hoàn toàn với lỗi ban đầu. Thông tin này giúp quyết định có nên tăng `reset_timeout_seconds` hay không.

---

## 3. Cache — Lý do lựa chọn thuật toán và tham số

### Tại sao dùng n-gram cosine thay vì Jaccard hoặc exact match?

**Exact match** bỏ lỡ các biến thể câu hỏi như "Tóm tắt chính sách hoàn tiền" vs "Tóm tắt chính sách hoàn tiền của bạn" — cùng ý nghĩa nhưng không khớp hoàn toàn.

**Jaccard similarity** (giao / hợp của tập từ) cũng thiếu vì không nắm bắt được độ gần nhau về cấu trúc ký tự. Ví dụ: "circuit breaker pattern" và "circuit breaker design" có Jaccard = 0.5 (2/4 từ chung), nhưng về mặt ký tự, 85% n-gram trùng nhau.

**N-gram cosine** kết hợp cả hai mức độ:
- **Word tokens**: nắm bắt ngữ nghĩa ("circuit", "breaker")
- **Character trigrams**: nắm bắt cấu trúc ký tự ("cir", "irc", "rcu", ...) — giúp phân biệt "2024" và "2026" ngay cả khi chỉ khác một ký tự

Kết quả kiểm thử thực tế:
```
similarity("circuit breaker pattern", "circuit breaker design") = 0.856  ✅
similarity("Summarize refund policy", "Summarize the refund policy") = 0.904  ✅ cache hit
similarity("hello", "completely different") = 0.0  ✅ miss đúng
```

### Tại sao `similarity_threshold = 0.92`?

Thử nghiệm với các ngưỡng khác:
- `0.85` → false hit: "chính sách hoàn tiền 2024" khớp với "chính sách hoàn tiền 2026" (score ≈ 0.937) và trả về câu trả lời sai về năm.
- `0.95` → quá chặt: "Tóm tắt chính sách hoàn tiền" không khớp với "Tóm tắt chính sách hoàn tiền của bạn" dù cùng nghĩa.
- `0.92` → cân bằng: chấp nhận biến thể từ ngữ nhỏ, từ chối câu hỏi khác nội dung.

Ngưỡng 0.92 được bổ sung thêm lớp bảo vệ false-hit cho số 4 chữ số (xem bên dưới).

### Tại sao cần phát hiện false-hit theo số 4 chữ số?

Hai câu hỏi "Chính sách hoàn tiền năm 2024" và "Chính sách hoàn tiền năm 2026" có độ tương đồng n-gram rất cao (> 0.92) vì chỉ khác nhau ở "2024" vs "2026". Tuy nhiên, đây là hai câu hỏi hoàn toàn khác nhau về nội dung — trả về câu trả lời của 2024 cho truy vấn 2026 là sai.

Hàm `_looks_like_false_hit()` kiểm tra nếu cả hai chuỗi có số 4 chữ số nhưng khác nhau → từ chối cache hit và ghi log:

```json
{"event": "cache.false_hit", "query": "refund policy for 2026",
 "cached_key": "refund policy for 2024", "score": 0.9375, "reason": "date_or_number_mismatch"}
```

### Tại sao chặn cache với truy vấn nhạy cảm?

Các truy vấn chứa từ như "password", "account balance", "SSN" không được lưu cache vì:
1. Câu trả lời phụ thuộc vào người dùng cụ thể — lưu cache và trả cho người dùng khác là rò rỉ dữ liệu.
2. Ngay cả khi trả đúng người, dữ liệu nhạy cảm không nên tồn tại trong cache với TTL 5 phút.

Regex `PRIVACY_PATTERNS` áp dụng cho cả `get()` và `set()` — đảm bảo không bao giờ lưu và không bao giờ đọc nhầm.

### Tại sao `ttl_seconds = 300`?

5 phút đủ dài để phục vụ các câu hỏi lặp lại trong một phiên làm việc điển hình (người dùng hỏi lại câu tương tự trong vòng 5 phút). Ngắn hơn (ví dụ 60 giây) giảm hit rate đáng kể. Dài hơn (ví dụ 1 giờ) tăng rủi ro cache stale với thông tin thay đổi nhanh.

---

## 4. Cấu hình hệ thống

| Tham số | Giá trị | Lý do chọn |
|---|---:|---|
| `failure_threshold` | 3 | Chịu được burst lỗi ngẫu nhiên; chỉ mở circuit khi lỗi liên tiếp có hệ thống |
| `reset_timeout_seconds` | 2 | Đủ để provider hồi phục; không giữ traffic ở backup quá lâu |
| `success_threshold` | 1 | Một lần probe thành công đủ để đóng circuit; tránh giữ HALF_OPEN lâu |
| `cache.ttl_seconds` | 300 | Đủ cho một phiên làm việc; tránh stale data quá lâu |
| `similarity_threshold` | 0.92 | Cân bằng giữa hit rate và độ chính xác; 0.85 gây false hit với năm khác nhau |
| `load_test.requests` | 100 | Mỗi kịch bản; tổng 300 yêu cầu đủ để ước tính P95/P99 ổn định |

---

## 5. SLO và kết quả đo lường

### Định nghĩa SLO

| Chỉ số | Mục tiêu SLO | Thực tế (bộ nhớ) | Thực tế (Redis) | Đạt? |
|---|---|---:|---:|---|
| Tính khả dụng | ≥ 99% | 97,33% | 99,67% | ❌ bộ nhớ / ✅ Redis |
| Độ trễ P95 | < 2 500 ms | 320,82 ms | 320,13 ms | ✅ cả hai |
| Tỷ lệ fallback thành công | ≥ 90% | 89,74% | 98,36% | ❌ bộ nhớ / ✅ Redis |
| Tỷ lệ cache hit | ≥ 10% | 60,00% | 72,67% | ✅ cả hai |
| Thời gian phục hồi | < 5 000 ms | 2 466 ms | 2 341 ms | ✅ cả hai |

**Tại sao bộ nhớ chưa đạt SLO khả dụng?** Mỗi kịch bản tạo một `build_gateway()` mới với cache trống. Kịch bản `primary_timeout_100` buộc toàn bộ traffic qua backup — nếu backup cũng có lúc lỗi và cache chưa warm, yêu cầu rơi vào static fallback. Redis giữ cache ấm qua các kịch bản nên tỷ lệ hit cao hơn, ít gọi provider hơn, ít lỗi hơn.

### Số liệu thực tế (cache bộ nhớ, 3 kịch bản × 100 yêu cầu)

| Chỉ số | Giá trị |
|---|---:|
| Tổng số yêu cầu | 300 |
| Tính khả dụng | 0,9733 |
| Tỷ lệ lỗi | 0,0267 |
| Độ trễ P50 (ms) | 276,29 |
| Độ trễ P95 (ms) | 320,82 |
| Độ trễ P99 (ms) | 325,68 |
| Tỷ lệ fallback thành công | 0,8974 |
| Tỷ lệ cache hit | 0,6000 |
| Số lần circuit mở | 9 |
| Thời gian phục hồi trung bình (ms) | 2 466 |
| Chi phí ước tính | $0,049098 |
| Chi phí tiết kiệm nhờ cache | $0,18 |

Xem chi tiết: `reports/metrics.json`

---

## 6. So sánh hiệu quả cache

Ba lần chạy với cùng tập truy vấn, chỉ thay đổi cấu hình cache.

| Chỉ số | Không có cache | Cache bộ nhớ | Cache Redis | Ghi chú |
|---|---:|---:|---:|---|
| Tính khả dụng | 0,9533 | 0,9733 | 0,9967 | Cache hấp thụ 60–73% tải → ít gọi provider → ít mở circuit |
| Độ trễ P50 (ms) | 286,04 | 276,29 | 281,22 | Cache hit trả về gần 0 ms, kéo P50 xuống |
| Độ trễ P95 (ms) | 320,47 | 320,82 | 320,13 | P95 bị chi phối bởi provider khi cache miss — tương đương |
| Tỷ lệ cache hit | 0,000 | 0,600 | 0,727 | Redis giữ cache ấm giữa các kịch bản; bộ nhớ reset mỗi lần |
| Chi phí ước tính | $0,1156 | $0,0491 | $0,0334 | Giảm 57% (bộ nhớ) và 71% (Redis) so với không có cache |
| Chi phí tiết kiệm | $0,000 | $0,180 | $0,218 | |
| Số lần circuit mở | 25 | 9 | 7 | Ít gọi provider hơn → ít lỗi tích lũy → ít mở circuit |

**Tại sao P95 gần như không thay đổi dù có cache?** P95 đo độ trễ của 5% yêu cầu chậm nhất. Cache hit có latency ≈ 0 ms, nhưng những yêu cầu này rơi vào nhóm nhanh nhất, không ảnh hưởng đến đuôi phân phối. P95 bị chi phối bởi các yêu cầu phải gọi provider (180–240 ms) hoặc thậm chí fallback backup (260 ms) — những trường hợp này vẫn tồn tại dù có cache.

---

## 7. Cache dùng chung với Redis

### Tại sao cache bộ nhớ không đủ cho môi trường nhiều instance

Trong triển khai production với nhiều replica, mỗi container giữ riêng một `ResponseCache` trong RAM. Hai instance nhận cùng một câu hỏi sẽ cả hai bị cache miss và gọi provider hai lần — lãng phí chi phí và tăng tải lên upstream. Hơn nữa, khi một instance khởi động lại (deploy mới, crash), toàn bộ cache warm bị mất.

### Cách `SharedRedisCache` giải quyết

`SharedRedisCache` lưu mỗi entry vào Redis với khóa xác định từ hash MD5 của câu truy vấn (`md5(query.lower().strip())[:12]`). Bất kỳ instance nào gọi `.set()` sẽ làm kết quả đó ngay lập tức khả dụng với mọi instance khác qua `.get()`. TTL được Redis quản lý tự động bằng `EXPIRE` — không cần eviction thủ công.

**Lý do dùng hash MD5 cho khóa** thay vì lưu nguyên văn câu hỏi: giới hạn độ dài khóa Redis, tránh vấn đề encoding, và đảm bảo hai câu hỏi giống nhau (sau normalize lower+strip) luôn cho cùng khóa.

**Lý do dùng Redis Hash** (`HSET key query value response value`) thay vì String đơn giản: cho phép lưu cả `query` gốc cùng với `response`, cần thiết cho bước quét similarity — phải đọc lại `query` gốc để so sánh với truy vấn mới.

### Bằng chứng chia sẻ trạng thái

```
PASSED tests/test_redis_cache.py::test_shared_state_across_instances

c1 = SharedRedisCache(...)   # Instance 1
c2 = SharedRedisCache(...)   # Instance 2 — đối tượng Python khác nhau hoàn toàn

c1.set("shared query", "shared response")
cached, _ = c2.get("shared query")
assert cached == "shared response"   # ✅ Instance 2 thấy dữ liệu của Instance 1
```

### Redis CLI — khóa còn tồn tại sau khi chạy chaos

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:fff10da1c72c
rl:cache:844ef0143a5c
rl:cache:9e413fd814eb
rl:cache:d354658dc020
rl:cache:3dab98c0e49e
rl:cache:734852f3cf4a
rl:cache:0bc3b1acf73d
rl:cache:b2a52f7dc795
rl:cache:8baa2cfa11fa
rl:cache:095946136fea
rl:cache:98332d0d1c9c
rl:cache:dacb2b833659

$ docker compose exec redis redis-cli HGETALL "rl:cache:fff10da1c72c"
1) "query"
2) "What are the admission requirements for international students?"
3) "response"
4) "[backup] reliable answer for: What are the admission requirements..."
```

### So sánh độ trễ: bộ nhớ vs Redis

| Chỉ số | Cache bộ nhớ | Cache Redis | Ghi chú |
|---|---:|---:|---|
| Độ trễ P50 (ms) | 276,29 | 281,22 | Redis thêm ~5 ms RTT mạng cho bước quét similarity |
| Độ trễ P95 (ms) | 320,82 | 320,13 | Tương đương — cả hai bị chi phối bởi provider khi miss |
| Tỷ lệ cache hit | 60,0% | 72,7% | Redis warm qua nhiều kịch bản → hit rate cao hơn |

Chi phí overhead ~5 ms của Redis là chấp nhận được khi đổi lại hit rate cao hơn 12% và tính khả dụng tăng từ 97,33% lên 99,67%.

---

## 8. Kịch bản Chaos

### Lý do chọn 3 kịch bản này

Ba kịch bản được chọn để kiểm tra ba chế độ lỗi khác nhau hoàn toàn:
- `primary_timeout_100`: kiểm tra khả năng **chịu lỗi hoàn toàn** — circuit có mở đúng không, fallback có tiếp quản không
- `primary_flaky_50`: kiểm tra **tính ổn định khi lỗi ngẫu nhiên** — circuit có dao động (flapping) không, hay ổn định
- `all_healthy`: thiết lập **baseline** để đo overhead của hệ thống reliability (circuit breaker, cache) so với gọi thẳng

| Kịch bản | Hành vi kỳ vọng | Hành vi quan sát | Pass/Fail |
|---|---|---|---|
| `primary_timeout_100` | Primary lỗi 100% → circuit mở sau 3 lần → toàn bộ traffic chuyển backup; static fallback < 5% | Circuit mở đúng sau lần lỗi thứ 3; backup xử lý phần lớn; sau timeout circuit thăm dò lại nhưng primary vẫn lỗi → mở lại ngay | ✅ Đạt |
| `primary_flaky_50` | Circuit dao động có kiểm soát: mở khi burst lỗi, probe ở HALF_OPEN, đóng khi probe thành công | Circuit mở và phục hồi nhiều lần; mix route `primary` + `fallback` + `cache_hit` trong log | ✅ Đạt |
| `all_healthy` | 100% qua primary; không có circuit mở; cache giảm tải dần theo thời gian | Primary xử lý toàn bộ yêu cầu live; circuit giữ CLOSED; cache hit rate tăng sau ~20 yêu cầu đầu | ✅ Đạt |

**Bằng chứng từ log** (`reports/trace.jsonl`):

```json
{"event": "scenario.start", "name": "primary_timeout_100"}
{"event": "breaker.opened",    "name": "primary", "reason": "failure_threshold_reached", "failure_count": 3}
{"event": "breaker.denied",    "name": "primary", "state": "open"}
{"event": "gateway.complete",  "route": "fallback", "provider": "backup", "latency_ms": 263.4}
{"event": "breaker.probe",     "name": "primary", "state": "half_open"}
{"event": "breaker.opened",    "name": "primary", "reason": "probe_failure", "failure_count": 1}
{"event": "gateway.complete",  "route": "cache_hit:1.00", "cache_hit": true, "latency_ms": 0}
{"event": "scenario.end",      "name": "primary_timeout_100", "availability": 0.95, "circuit_opens": 4}
```

Log cho thấy hệ thống hoạt động đúng: circuit mở → traffic chuyển backup → probe thất bại → mở lại → cache phục vụ những câu hỏi đã lưu mà không cần gọi provider.

---

## 9. Phân tích điểm yếu còn lại

**Vấn đề chính: trạng thái circuit breaker không được chia sẻ giữa các instance.**

Hiện tại mỗi `ReliabilityGateway` giữ riêng circuit breaker trong bộ nhớ. Trong môi trường production với 3 replica:
- Instance A thấy primary lỗi 3 lần → mở circuit, bắt đầu gửi traffic sang backup.
- Instance B và C vẫn gửi traffic sang primary vì chúng chưa đủ 3 lần lỗi riêng.
- Kết quả: primary vẫn nhận tải từ B, C trong khi đang lỗi → không giảm được áp lực, và retry storm có thể xảy ra.

**Biểu hiện trong metrics hiện tại:** `circuit_open_count = 9` (3 kịch bản × ~3 lần mở/kịch bản) tất cả đến từ một gateway duy nhất. Trong môi trường nhiều instance, con số này sẽ nhân lên theo số replica.

**Cách khắc phục:** Lưu `failure_count`, `state`, `opened_at` trong Redis hash theo tên breaker. Dùng `WATCH` + `MULTI/EXEC` để chuyển trạng thái nguyên tử:

```
HSET breaker:primary failure_count 3 state open opened_at 1782882991.0
EXPIRE breaker:primary 60
```

Mọi instance đọc cùng trạng thái từ Redis → circuit đồng bộ → không còn retry storm.

---

## 10. Các bước cải thiện tiếp theo

1. **Chia sẻ trạng thái circuit breaker qua Redis** — như phân tích ở trên; ưu tiên cao nhất trước khi đưa lên production nhiều instance.

2. **Định tuyến theo ngân sách chi phí** — theo dõi tổng chi phí tích lũy trong gateway; khi chạm 80% ngưỡng ngân sách, ưu tiên cache và backup (rẻ hơn 40%); khi đạt 100%, trả về static fallback mà không gọi bất kỳ provider nào. Tính năng này ngăn chi phí vượt kiểm soát trong các tình huống tải đột biến.

3. **Cảnh báo vi phạm SLO tự động** — sau mỗi kịch bản, so sánh `availability` và `latency_p95_ms` với mục tiêu trong config; ghi sự kiện `slo.violation` vào `trace.jsonl` và để `make run-chaos` thoát với mã lỗi 1 nếu vi phạm. Điều này biến báo cáo thủ công thành gate tự động trong CI/CD.
