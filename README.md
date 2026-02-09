# MiniCut (Flet) — MVP ตัด/แบ่ง/ลากวาง + Export ด้วย FFmpeg

> เป้าหมาย: หน้าตาคล้าย CapCut แบบเบาๆ (V1/A1)  
> ฟีเจอร์: Import → ลากลง Timeline → Split → จัดลำดับ → Export mp4

## Requirements
- Windows 10/11
- Python 3.10+
- FFmpeg (ffmpeg.exe + ffprobe.exe)  
  - ใส่ไว้ที่ `bin/ffmpeg.exe` และ `bin/ffprobe.exe` **หรือ** ให้มีใน PATH

## ติดตั้ง/รัน
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## วิธีใช้ (สั้นๆ)
1) กด **Import** เลือกไฟล์ mp4/mov/mkv
2) ลากไฟล์จาก Media Bin ลง Timeline (V1/A1)
3) เลือกคลิปบน Timeline → ปรับ Split slider → กด **Split**
4) ลากคลิปเพื่อสลับลำดับ/แทรกก่อนคลิปอื่น
5) กด **Export** → ได้ไฟล์ mp4

## Build เป็น .exe (Windows)
ใช้ `flet pack` (CLI ของ Flet ที่แพ็กเป็น executable ได้)
```powershell
.\build_windows.ps1
```

หรือรันเอง:
```powershell
.\.venv\Scripts\flet.exe pack app.py -n MiniCut -D --add-binary "bin\ffmpeg.exe:bin\ffmpeg.exe:win32" --add-binary "bin\ffprobe.exe:bin\ffprobe.exe:win32"
```

> หมายเหตุ: ถ้ายังไม่มี ffmpeg/ffprobe ให้ดาวน์โหลด FFmpeg แล้วคัดลอก exe มาใส่ในโฟลเดอร์ `bin/`

## Tests (core logic)
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Edge cases
- ไฟล์ไม่มี audio stream: export จะสร้างเสียงเงียบให้อัตโนมัติ (anullsrc)
- ไฟล์ VFR/แปลกมาก: split อาจคลาดเคลื่อนเล็กน้อย (ขึ้นกับ ffmpeg)

## Big-O
- split / move clip: O(n)
- build export command: O(n)
