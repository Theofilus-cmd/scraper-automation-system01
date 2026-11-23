PRD Teknis — Scraper Automation System
Dokumen ini merangkum sistem SaaS otomasi scraping berbasis web yang sedang dibangun pada branch phase3. Fokus implementasi saat ini adalah fondasi backend: scheduler durable tasks, worker Celery, politeness per-domain, observability, dan frontend monitoring.

1. Ringkasan Produk
Aplikasi ini adalah platform SaaS untuk menjalankan pekerjaan scraping secara terjadwal, tervalidasi, dan dapat diamati. Pengguna membuat task scraping, sistem menjadwalkannya, worker mengeksekusinya dengan batasan politeness per-domain, lalu hasil dan status eksekusi tersimpan untuk monitoring.

Sistem dirancang untuk operasi multi-tenant, dengan autentikasi JWT, isolasi data per user, dan antrean eksekusi yang tahan terhadap retry. Prinsip utamanya adalah durable task sebagai sumber kebenaran status, bukan keberadaan pesan di broker.

2. Arsitektur Sistem
2.1 Komponen utama
Komponen	Teknologi	Peran
Frontend	Next.js/React, port 3000	Dashboard, autentikasi, manajemen task, monitoring
API	FastAPI/Uvicorn, port 8000	Autentikasi, CRUD resource, validasi, orkestrasi task
Scheduler	Python service	Klaim jadwal due, heartbeat, rekonsiliasi, retention
HTTP worker	Celery worker	Eksekusi scraping HTTP, integrasi politeness, persistence
PostgreSQL	PostgreSQL 16 + PgBouncer	Data aplikasi, task, run, observasi, user
Redis	Redis 7	Broker Celery, lease eksekusi, rate limit, cooldown, semaphore
Mock Store	FastAPI, port 4000	Target scraping internal untuk pengembangan dan integration test
MailHog	MailHog, port 8025	Email development/testing
2.2 Alur eksekusi
User login melalui frontend dan memperoleh JWT.

User membuat atau mengaktifkan task scraping melalui API.

Scheduler memeriksa task yang due, lalu membuat run eksekusi.

Scheduler mengirim pekerjaan ke Celery/Redis.

Worker mengklaim pekerjaan dan memperoleh execution lease di Redis.

Worker memvalidasi target, menjalankan fetch, dan menerapkan politeness per-domain.

Worker menyimpan observasi, status run, error, retry, dan dead-letter bila diperlukan.

Frontend menampilkan status task, run, dan metrik operasional.

2.3 Prinsip reliability
Durable task adalah sumber kebenaran. Kehadiran pesan di Redis bukan jaminan status akhir.

Execution lease mencegah dua worker aktif mengeksekusi task yang sama secara bersamaan, tetapi bukan exactly-once guarantee.

Redis outage bersifat fail-closed untuk koordinasi: worker tidak boleh melanjutkan eksekusi bila tidak dapat memastikan kepemilikan lease atau koordinasi domain.

Retry terbatas dan terukur. Kegagalan persisten berakhir di dead-letter setelah batas attempt.

Observability wajib. Setiap eksekusi menghasilkan run, observasi, status, dan jejak kegagalan yang dapat dipantau.

3. Modul Backend
3.1 Autentikasi dan user
Registrasi dan login user.

JWT access token dengan algoritma HS256.

JWT_SECRET_KEY wajib disediakan melalui environment; tidak boleh memakai default development secret di produksi.

Data dan resource aplikasi diisolasi per user.

Access token default kedaluwarsa dalam 30 menit.

3.2 Task scraping
Task adalah definisi pekerjaan scraping yang dimiliki user. Task menyimpan target, konfigurasi eksekusi, jadwal, status, dan hubungan ke run.

Aturan inti:

Task harus dimiliki oleh user pembuatnya.

Task dapat dijadwalkan atau dieksekusi sesuai kebutuhan.

Scheduler hanya memproses task yang due.

Task yang sudah terminal tidak boleh dieksekusi ulang tanpa alur bisnis yang jelas.

Perubahan task tidak boleh merusak riwayat run yang sudah ada.

3.3 Scheduler
Scheduler menjalankan loop periodik dengan konfigurasi berikut:

Parameter	Nilai default
Heartbeat interval	15 detik
Claim interval	10 detik
Claim batch size	20
Reconciliation interval	120 detik
Retention purge interval	3600 detik
History retention	90 hari
Tanggung jawab scheduler:

Memeriksa heartbeat database dan Redis.

Mengklaim schedule yang due secara atomic.

Membuat run eksekusi.

Mengirim pekerjaan ke worker.

Melakukan rekonsiliasi task/run yang tertinggal.

Menjalankan retention purge untuk observasi, task, dan run lama.

Log scheduler yang sudah aktif menunjukkan:

text
scheduler starting
scheduler heartbeat db_ok=true redis_ok=true
scheduler claimed due schedules
retention purge completed
3.4 Worker HTTP
Worker mengeksekusi task scraping melalui Celery. Ia bertanggung jawab atas validasi target, fetch HTTP, persistence hasil, retry, dan koordinasi politeness.

Aturan worker:

Worker harus memperoleh execution lease sebelum mengeksekusi task.

Worker harus memvalidasi URL target untuk mencegah SSRF.

Worker harus menyimpan observasi hasil fetch.

Worker harus menangani retry dan dead-letter.

Worker harus menghormati rate limit, cooldown, dan concurrency per-domain.

Worker tidak boleh menganggap lease tetap valid selama request berjalan; lease harus diperbarui dan diverifikasi.

4. Politeness dan Koordinasi Domain
4.1 Tujuan
Sistem membatasi beban terhadap target eksternal agar scraping tetap sopan, dapat diprediksi, dan tidak menimbulkan lonjakan request pada satu hostname.

Konfigurasi aktif:

Parameter	Nilai default
task_execution_lease_seconds	60
domain_max_concurrency	3
domain_rate_limit_seconds	2
domain_cooldown_seconds	600
4.2 Execution lease
Execution lease adalah lock per task berbasis Redis.

Key: scraper:task-execution:<task_id>

Token: UUID acak per attempt.

TTL: 60 detik secara default.

Acquire memakai SET key token EX ttl NX.

Renew dan release memakai Lua compare-and-act.

Worker lama tidak dapat memperpanjang atau menghapus lease milik worker baru.

Redis gagal berarti CoordinationUnavailableError; worker harus berhenti, bukan melanjutkan tanpa koordinasi.

4.3 Domain semaphore
Domain semaphore membatasi jumlah request aktif per hostname.

Key: scraper:domain-semaphore:<hostname>

Struktur: Redis sorted set.

Member: token kepemilikan.

Score: timestamp kedaluwarsa slot dalam milidetik.

Kapasitas: domain_max_concurrency, default 3.

Slot kedaluwarsa dibersihkan sebelum acquire.

Acquire hanya berhasil bila jumlah slot aktif di bawah kapasitas.

Renew hanya berhasil bila token masih ada.

Release hanya berhasil bila token masih dimiliki.

Redis gagal berarti fail-closed.

Implementasi sudah ada di:

text
backend/app/workers/domain_semaphore.py
backend/tests/test_domain_semaphore.py
Commit checkpoint:

text
7fc23e6 feat(politeness): add Redis domain semaphore
4.4 Rate limit per-domain
Rate limit mencegah request terlalu rapat pada satu hostname.

Jeda minimum antar-request: domain_rate_limit_seconds, default 2 detik.

Implementasi berbasis Redis.

Worker harus menunggu atau gagal aman bila limit belum tersedia.

Rate limit harus bekerja bersama semaphore, bukan menggantikannya.

4.5 Cooldown domain
Cooldown dipakai setelah kegagalan tertentu, khususnya kondisi server yang menyarankan backoff.

Key cooldown per hostname.

TTL default: 600 detik.

Cooldown dapat dibuat.

Cooldown dapat diperpanjang.

Cooldown tidak boleh dipendekkan oleh update berikutnya.

Update bersamaan harus aman.

HTTP 503 persisten setelah tiga attempt harus berakhir dead-letter.

Parsing header Retry-After belum diimplementasikan.

Implementasi dan test sudah ada di:

text
backend/app/workers/domain_rate_limit.py
backend/tests/test_domain_cooldown.py
backend/tests/test_domain_rate_limit.py
Commit checkpoint:

text
1d91839 test(politeness): verify domain cooldown with real Redis
5. Struktur Database
5.1 Entitas inti
Entitas	Peran
users	Akun pengguna dan identitas autentikasi
tasks	Definisi pekerjaan scraping milik user
runs	Satu eksekusi task
task_observations	Observasi hasil, status, error, atau metrik eksekusi
schedules	Jadwal eksekusi task
dead_letter	Task/run yang gagal permanen setelah retry
5.2 Relasi logis
text
users 1---N tasks
tasks 1---N schedules
tasks 1---N runs
runs 1---N task_observations
tasks/runs 1---N dead_letter_records
5.3 Aturan data
Setiap resource user-owned harus memiliki user_id.

Task harus dapat dilacak dari schedule, run, observasi, dan dead-letter.

Run harus menyimpan status eksekusi dan waktu.

Observasi harus menyimpan hasil atau kegagalan granular.

Dead-letter harus menyimpan alasan kegagalan dan konteks attempt.

Retention harus menghapus observasi, task, dan run lama tanpa merusak integritas data aktif.

6. Aturan Bisnis
6.1 Eksekusi task
Task hanya boleh dieksekusi oleh worker yang memegang execution lease.

Dua worker tidak boleh aktif mengeksekusi task yang sama secara bersamaan.

Lease kedaluwarsa tidak otomatis berarti request boleh dilanjutkan; worker harus memverifikasi kepemilikan sebelum melanjutkan.

Jika lease hilang saat request masih berjalan, worker harus menghentikan atau membatalkan request.

Task yang gagal dapat diretry sesuai policy.

Kegagalan persisten setelah batas attempt harus masuk dead-letter.

6.2 Politeness
Maksimum request aktif per hostname adalah 3.

Jeda minimum antar-request per hostname adalah 2 detik.

Cooldown default setelah kondisi tertentu adalah 600 detik.

Cooldown tidak boleh dipendekkan.

HTTP 503 persisten setelah tiga attempt harus dead-letter.

Parsing Retry-After harus ditambahkan sebagai bagian dari politeness.

6.3 Keamanan
JWT secret wajib berasal dari environment.

Target URL harus divalidasi untuk mencegah SSRF.

Hostname yang resolve ke private, loopback, atau link-local address harus diblokir.

Mock Store internal tetap diizinkan sebagai dependency development.

Redis dan database harus dianggap dependensi kritikal; kegagalan koneksi harus fail-closed untuk operasi koordinasi.

6.4 Retensi
History retention default: 90 hari.

Retention purge berjalan setiap 3600 detik.

Retention dapat menghapus observasi, task, dan run lama.

Retention tidak boleh menghapus data aktif atau data yang masih diperlukan untuk audit.

7. Desain UI/UX
7.1 Prinsip
Frontend harus sederhana, operasional, dan berfokus pada monitoring. User harus dapat memahami kondisi task dan run tanpa membuka log backend.

Prinsip UI:

Status harus jelas: idle, scheduled, running, succeeded, failed, dead-letter.

Error harus dapat dibaca, bukan hanya stack trace mentah.

Setiap task harus menampilkan riwayat run.

Setiap run harus menampilkan observasi dan penyebab kegagalan.

Aksi destruktif harus meminta konfirmasi.

Loading, empty state, error state, dan unauthorized state wajib ada.

7.2 Halaman utama
Halaman	Tujuan
Login/Register	Autentikasi user
Dashboard	Ringkasan task, run, keberhasilan, dan kegagalan
Tasks	Daftar dan manajemen task
Task Detail	Konfigurasi, schedule, run, dan status task
Run Detail	Observasi, error, retry, dan hasil eksekusi
Dead Letter	Daftar kegagalan permanen dan penyebabnya
Settings	Konfigurasi akun dan preferensi user
7.3 Komponen penting
Task card dengan status dan waktu eksekusi berikutnya.

Run timeline.

Badge status.

Error panel.

Retry indicator.

Dead-letter indicator.

Form validasi inline.

Empty state.

Loading skeleton.

Toast/notifikasi hasil aksi.

8. API
8.1 Auth
Endpoint	Metode	Fungsi
/auth/register	POST	Registrasi user
/auth/login	POST	Login dan terbit JWT
/auth/me	GET	Profil user aktif
8.2 Task dan run
Endpoint	Metode	Fungsi
/tasks	GET	Daftar task user
/tasks	POST	Buat task
/tasks/{id}	GET	Detail task
/tasks/{id}	PATCH	Update task
/tasks/{id}	DELETE	Hapus task
/tasks/{id}/runs	GET	Riwayat run task
/runs/{id}	GET	Detail run
/runs/{id}/observations	GET	Observasi run
Endpoint di atas adalah kontrak fungsional yang menjadi target implementasi; detail path final harus mengikuti router yang sudah ada di backend.

9. Fitur yang Sudah Selesai
9.1 Infrastruktur
Docker Compose dengan PostgreSQL, PgBouncer, Redis, API, scheduler, worker, frontend, mock store, dan MailHog.

Health check service.

Script scripts/wait_for_healthy.sh.

Scheduler heartbeat dengan pemeriksaan db_ok dan redis_ok.

Retention purge aktif.

Scheduler claim due schedules aktif.

9.2 Backend
Autentikasi JWT.

Konfigurasi Phase 3 untuk lease dan politeness.

Execution lease Redis token-based.

Renew dan release lease berbasis Lua.

CoordinationUnavailableError dan LeaseOwnershipLostError.

Durable task dan persistence state.

Retry dan dead-letter untuk HTTP 503 persisten setelah tiga attempt.

Domain rate limit Redis.

Domain cooldown Redis.

Domain semaphore Redis.

Validasi target untuk SSRF hardening.

9.3 Testing
Integration test cooldown Redis nyata.

Integration test semaphore Redis nyata.

Test kapasitas semaphore.

Test kepemilikan token.

Test renewal dan release.

Test expiry member dan expiry key.

Test concurrent update cooldown.

Test cooldown tidak boleh dipendekkan.

Test rate limit.

128 integration tests lulus pada checkpoint a0d93ad.

31 test politeness lulus pada checkpoint 7fc23e6.

9.4 Checkpoint commit
text
a0d93ad test(politeness): verify persistent HTTP 503 dead-letter
1d91839 test(politeness): verify domain cooldown with real Redis
7fc23e6 feat(politeness): add Redis domain semaphore
10. Fitur yang Belum Selesai
10.1 Prioritas tinggi
Fitur	Status	Keterangan
Integrasi semaphore ke request worker	Belum	Semaphore sudah ada, tetapi belum dipakai worker saat fetch
Supervised lease renewal	Belum	Worker harus memperbarui lease secara berkala selama request berjalan
Pembatalan request saat lease hilang	Belum	Request harus dihentikan bila kepemilikan lease tidak lagi valid
Integrasi cooldown ke request worker	Belum	Cooldown harus memblokir fetch saat hostname sedang cooldown
Integrasi rate limit ke request worker	Belum	Worker harus menghormati jeda minimum per-domain
Parsing Retry-After	Belum	Header harus diparse dan dipakai untuk backoff
Cleanup semaphore pada fetch selesai	Belum	Slot harus dilepas saat sukses, gagal, atau dibatalkan
Dead-letter dari politeness failure	Perlu diverifikasi	Alur kegagalan politeness harus konsisten dengan retry/dead-letter
10.2 Prioritas menengah
Fitur	Status	Keterangan
Dashboard metrik operasional	Belum lengkap	Success rate, failure rate, retry, dead-letter
Halaman dead-letter UI	Belum lengkap	User harus dapat melihat penyebab kegagalan
Halaman run detail UI	Belum lengkap	Observasi dan status run harus terlihat jelas
Notifikasi kegagalan	Belum	Email atau notifikasi saat task masuk dead-letter
Manajemen schedule UI	Belum lengkap	User harus dapat mengatur jadwal task
Audit log	Belum	Perubahan task dan aksi user perlu tercatat
Rate limiting API	Belum	Endpoint publik perlu dilindungi dari abuse
10.3 Prioritas lanjutan
Fitur	Status	Keterangan
Multi-tenant quota	Belum	Batas task, run, dan resource per user
Billing/plan	Belum	Paket SaaS dan pembatasan penggunaan
Webhook notifikasi	Belum	Notifikasi eksternal saat run selesai/gagal
Export hasil scraping	Belum	CSV/JSON export
Retry policy per-task	Belum	Konfigurasi retry granular per task
Observability dashboard	Belum	Metrik worker, queue, dan politeness
Deployment produksi	Belum	Secret management, monitoring, backup, dan CI/CD produksi
11. Urutan Kerja Berikutnya
Integrasi domain_semaphore ke request worker.

Tambahkan supervised lease renewal selama request berjalan.

Hentikan request bila lease ownership hilang.

Integrasi rate limit dan cooldown ke alur fetch.

Lepas semaphore slot pada fetch sukses, gagal, dan dibatalkan.

Implementasi parsing Retry-After.

Pastikan retry dan dead-letter konsisten untuk semua jenis kegagalan politeness.

Tambahkan integration test end-to-end untuk worker + politeness.

Lengkapi UI monitoring task, run, dan dead-letter.

Tambahkan observability dan alerting.

12. Definisi Selesai
Fitur politeness dianggap selesai bila:

Worker memperoleh semaphore sebelum fetch.

Worker menghormati rate limit per-domain.

Worker tidak mengeksekusi fetch saat hostname cooldown.

Lease diperbarui selama request berjalan.

Request dihentikan bila lease hilang.

Semaphore dilepas pada semua jalur keluar: sukses, gagal, timeout, dan pembatalan.

Retry-After diparse dan dipakai.

HTTP 503 persisten berakhir dead-letter.

Semua jalur memiliki integration test Redis nyata.

Ruff, mypy, dan test terkait lulus.
