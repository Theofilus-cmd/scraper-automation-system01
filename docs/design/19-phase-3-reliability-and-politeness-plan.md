# 19 — Phase 3 Reliability & Politeness Plan

## Tujuan

Phase 3 membuat scraper berjalan otomatis secara berkala dengan aman,
andal, dan sopan terhadap website target.

Model yang dipertahankan:

    satu source -> satu schedule -> satu run -> satu task

Phase 3 memperkuat mesin yang sudah ada. Phase ini tidak menambah adapter
baru, billing, email alert, export, Playwright, atau redesign multi-task.

## Hasil akhir

Sistem harus dapat:

- Menjalankan source aktif sesuai jadwal.
- Memulihkan task yang tersangkut atau hilang dari queue.
- Mencegah task ganda melakukan fetch, history, atau counter ganda.
- Retry kegagalan sementara secara aman.
- Berhenti retry untuk kegagalan permanen.
- Membatasi request per domain.
- Menghormati Retry-After dan memperlambat domain yang membatasi request.

## Perlindungan task ganda

Celery dapat mengirim task yang sama lebih dari sekali ketika worker mati
atau acknowledgement ke queue gagal. Ini harus aman.

Setiap task memakai idempotency key dari run dan source. Sebelum fetch,
worker mengklaim key tersebut secara atomik di Redis dengan TTL aman.
Jika klaim gagal, worker berhenti tanpa fetch ulang dan tanpa menambah
history atau counter run.

Database tetap menjadi perlindungan terakhir: task terminal tidak boleh
difinalkan dua kali, dan counter sukses/gagal run hanya boleh bertambah
satu kali untuk setiap task.

## Retry dan error

Task mendapat maksimum tiga attempt: satu attempt awal dan dua retry.
Retry memakai jeda yang bertambah, maksimal 60 detik, serta jitter.

Timeout, network error, HTTP 5xx, HTTP 429, dan HTTP 503 dapat di-retry.
HTTP 404, SSRF/DNS blocked, source archived, adapter tidak didukung,
parse gagal, dan data wajib tidak valid adalah kegagalan permanen.

Status retry harus terlihat benar di database:

    in_progress -> retrying -> queued

## Perlindungan task ganda

Celery dapat mengirim task yang sama lebih dari sekali ketika worker mati
atau acknowledgement ke queue gagal. Ini harus aman.

Setiap task memakai idempotency key dari run dan source. Sebelum fetch,
worker mengklaim key tersebut secara atomik di Redis dengan TTL aman.
Jika klaim gagal, worker berhenti tanpa fetch ulang dan tanpa menambah
history atau counter run.

Database tetap menjadi perlindungan terakhir: task terminal tidak boleh
difinalkan dua kali, dan counter sukses/gagal run hanya boleh bertambah
satu kali untuk setiap task.

## Retry dan error

Task mendapat maksimum tiga attempt: satu attempt awal dan dua retry.
Retry memakai jeda yang bertambah, maksimal 60 detik, serta jitter.

Timeout, network error, HTTP 5xx, HTTP 429, dan HTTP 503 dapat di-retry.
HTTP 404, SSRF/DNS blocked, source archived, adapter tidak didukung,
parse gagal, dan data wajib tidak valid adalah kegagalan permanen.

Status retry harus terlihat benar di database:

    in_progress -> retrying -> queued

## Kesopanan domain

Sebelum melakukan request HTTP, worker membatasi aktivitas untuk setiap
domain agar sistem tidak membanjiri website target.

Default awal:

- Maksimum 3 request bersamaan per domain.
- Maksimum 1 request setiap 2 detik per domain.
- Cooldown 10 menit setelah domain memberi HTTP 429 atau HTTP 503.

Jika website memberi header Retry-After, worker menghormati waktu tunggu
tersebut saat menjadwalkan retry. Jika Redis tidak tersedia, scraper tidak
boleh mengabaikan pembatas domain; pekerjaan harus gagal aman atau ditunda.

Semaphore domain harus selalu dilepas walaupun fetch gagal. Perlindungan
SSRF, pemeriksaan redirect, dan timeout yang sudah ada tidak boleh
dilemahkan.

## Recovery scheduler

Scheduler yang sudah ada tetap digunakan untuk membuat run dari schedule
yang sudah due dan untuk memulihkan pekerjaan macet.

Task queued atau in_progress yang melewati batas stuck time dikirim ulang
dengan idempotency key yang sama. Karena ada perlindungan task ganda,
pengiriman ulang ini tidak boleh membuat hasil, history, atau counter dobel.

## Test wajib

Phase 3 harus memiliki test untuk:

- Task ganda tidak melakukan fetch atau finalisasi dua kali.
- Counter run tidak bertambah dua kali.
- Retry memperbarui status dan waktu queue dengan benar.
- HTTP 404 gagal tanpa retry.
- HTTP 429/503 memakai Retry-After dan mengaktifkan cooldown domain.
- HTTP 5xx, timeout, dan network error melakukan retry.
- Semaphore domain selalu dilepas setelah sukses atau gagal.
- Rate limit domain membatasi request berulang.
- Reconciliation dapat mengirim ulang task dengan aman.
- Semua test SSRF, redirect safety, source lifecycle, run history, dan
  retention dari fase sebelumnya tetap lulus.

## Kriteria selesai

Phase 3 selesai bila test backend, lint, dan typecheck lulus; Docker Compose
tetap dapat start; scheduler dapat memicu source yang jadwalnya due; worker
menyimpan hasil scrape normal; retry dan dead-letter dapat dibuktikan; serta
duplicate dispatch tidak membuat fetch, history, atau progress counter ganda.

Phase 3 tidak mencakup adapter baru, browser automation, email alert, diff
engine, billing, export, API key, remote Git hosting, atau redesign menjadi
multi-task run.

## Amendment (scope extension, approved by product owner)

Phase 3 continues past the original non-goals into an MVP push, in order:
(A) wire politeness into the worker, (B) config/security hardening,
(C) Shopify adapter, (D) price/stock alerts with email, (E) frontend
dashboard, (F) end-to-end validation. Amazon is explicitly deferred.

### Module A implementation notes

- `app/workers/politeness.py::polite_fetch` wraps each fetch: cooldown check,
  domain semaphore, rate slot, then fetch under a deadline of 75% of the
  lease TTL; the semaphore slot is released on success, failure, timeout
  and cancellation. Redis failure is fail-closed.
- A politeness deferral (`domain_cooldown`, `domain_busy`,
  `domain_rate_limited`) hands the durable attempt back, re-queues the row,
  and re-dispatches with a Celery countdown. After 20 deferrals the task is
  dead-lettered.
- HTTP 429/503 start/extend the per-host cooldown (never shortened) using
  `Retry-After` when larger than the default.
- Politeness is keyed on the source URL host; redirect hops are SSRF-checked
  but not individually throttled.
