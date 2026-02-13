# MiniCut (MVP)

MiniCut เป็นโปรแกรมตัดต่อวิดีโอแบบเบาๆ (single timeline) พัฒนาด้วย Flet + FFmpeg
เหมาะกับงานตัดคลิปเร็วๆ เช่น นำไฟล์เข้ามา, แบ่งคลิป, จัดลำดับ, และ export เป็น `.mp4`

## รายงานความสามารถของโปรแกรม

อ้างอิงจากโค้ดปัจจุบันใน `app.py` และโมดูลใน `core/`

### 1) จัดการไฟล์สื่อ (Media Bin)
- Import ไฟล์ได้หลายไฟล์พร้อมกัน
- รองรับไฟล์วิดีโอ/เสียง: `mp4 mov mkv avi webm flv wmv m4v mp3 wav flac aac ogg m4a`
- อ่าน metadata ด้วย `ffprobe` เช่น duration, resolution, fps, codec, bitrate, sample rate, channels
- แสดงข้อมูลไฟล์ใน Media Bin และลากลง timeline ได้

### 2) ตัดต่อบน Timeline
- มี 2 แทร็กหลัก: `V1` (video) และ `A1` (audio)
- ลาก media ลงแทร็กได้ และลากคลิปเพื่อ reorder ได้
- คลิกเลือกคลิปบน timeline ได้
- Split clip ได้ 2 แบบ
  - ปรับตำแหน่งจาก slider ใน Inspector
  - คลิก/ลากบนบล็อกคลิปเพื่อกำหนดจุด split แบบ visual
- แสดงเส้น marker สีแดงบนคลิปที่เลือก (ตำแหน่ง split)
- Trim คลิปด้วยการตั้ง `in/out`
- Duplicate / Delete คลิปได้
- ปรับ Zoom timeline ได้

### 3) Inspector และการปรับเสียงต่อคลิป
- แสดงข้อมูลคลิปที่เลือก: track, in/out, duration
- ปรับค่า split, trim in/out
- ปรับเสียงรายคลิปได้ (volume, mute) เพื่อใช้ตอน export

### 4) Undo / Redo และคีย์ลัด
- Undo/Redo มี history manager
- คีย์ลัดหลัก:
  - `Ctrl+Z` = Undo
  - `Ctrl+Y` หรือ `Ctrl+Shift+Z` = Redo
  - `Delete/Backspace` = ลบคลิปที่เลือก
  - `S` = Split
  - `Ctrl+S` = Save
  - `Ctrl+I` = Import
  - `Ctrl+D` = Duplicate
  - `Ctrl+E` = Export
  - `+` / `-` = Zoom timeline
  - `Left` / `Right` = เลือกคลิปก่อนหน้า/ถัดไป

### 5) Project management
- Save / Save As / Load project (`.json`)
- เก็บ Recent projects ใน `~/.minicut/config.json`
- Auto-save ตามช่วงเวลา (ค่า default 60 วินาที)
- เปิดโปรเจกต์ล่าสุดอัตโนมัติเมื่อเริ่มโปรแกรม (ถ้ามี)

### 6) Export
- Export เป็น `.mp4` ด้วย FFmpeg
- รองรับโหมดเสียงตอน export:
  - `mix` = ผสมเสียงจาก V1 + A1
  - `a1_only` = ใช้เสียง A1 เท่านั้น
  - `v1_only` = ใช้เสียงจากคลิป V1 เท่านั้น
- ทำ normalize timeline เป็นลำดับเส้นตรง (linear timeline)

### 7) โหมดทดสอบระบบอัตโนมัติ (System Test)
- มีโหมดทดสอบในตัวผ่าน environment variables (`MINICUT_SYSTEM_TEST=1`)
- flow ทดสอบ: import 2 คลิป -> วาง timeline -> split ครึ่ง -> เก็บครึ่งแรก -> export
- รองรับการแคปภาพขั้นตอนทดสอบ (best-effort) ผ่าน screenshot helper script

### 8) รองรับการรันแบบ Desktop และ Browser
- Desktop: ใช้งานครบทุกฟีเจอร์
- Browser (`flet run --web`): ใช้งาน timeline/edit/export ได้
- หมายเหตุ: preview วิดีโอจาก local path ใน browser ถูกปิดไว้ตามข้อจำกัดของเบราว์เซอร์

## ข้อจำกัดปัจจุบัน (MVP)
- โครงสร้าง timeline เป็นแบบเรียงต่อกัน (linear) ไม่ใช่ multitrack ซับซ้อน
- มีเพียง 1 video track (`V1`) และ 1 audio track (`A1`)
- ยังไม่มี transition/effect/overlay/caption แบบเต็มรูปแบบ
- ความแม่นยำจุดตัดขึ้นกับการประมวลผลของ FFmpeg และ source media

## Requirements
- Windows 10/11
- Python 3.10+
- FFmpeg (`ffmpeg.exe`, `ffprobe.exe`)
  - วางไว้ที่ `bin/ffmpeg.exe` และ `bin/ffprobe.exe`
  - หรือให้เครื่องมองเห็นได้จาก `PATH`

## ติดตั้งและรัน

### 1) Desktop mode
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

### 2) Browser mode
```powershell
.\.venv\Scripts\flet.exe run --web --port 8571 app.py
```
เปิดเบราว์เซอร์ไปที่ `http://127.0.0.1:8571`

## Quick workflow
1. กด `Import`
2. ลากไฟล์จาก Media Bin ลง `V1` หรือ `A1`
3. คลิกคลิปที่ต้องการแก้
4. ตั้งจุด split/trim จาก Inspector หรือคลิกบนคลิป
5. กด `Split` / `Apply Trim` / `Duplicate` / `Delete` ตามต้องการ
6. กด `Export`

## Unit tests
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

ครอบคลุม core logic เช่น timeline operations, project model, history, config, ffprobe parsing

## Build Windows executable
```powershell
.\build_windows.ps1
```

หรือรันโดยตรง:
```powershell
.\.venv\Scripts\flet.exe pack app.py -n MiniCut -D --add-binary "bin\ffmpeg.exe:bin\ffmpeg.exe:win32" --add-binary "bin\ffprobe.exe:bin\ffprobe.exe:win32"
```
