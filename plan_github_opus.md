# MiniCut — แผนพัฒนา 10 ฟีเจอร์ (Opus Plan)

> สร้างจากการวิเคราะห์โค้ดทั้งโปรเจกต์ เมื่อ 2026-02-09  
> โปรเจกต์: **MiniCut (Flet)** — MVP ตัดต่อวิดีโอแบบเบาๆ คล้าย CapCut  
> Stack: Python + Flet 0.80.5 + flet-video/flet-audio + FFmpeg (subprocess)

---

## สถานะปัจจุบัน (Baseline)

| ส่วน | สิ่งที่มีแล้ว |
|---|---|
| **UI** | Media Bin, Timeline (V1/A1), Inspector, Preview placeholder |
| **Core Logic** | Import (probe), Split, Move/Reorder, Delete, Add clip |
| **Export** | FFmpeg concat filter (libx264 + aac 192k) — blocking, ไม่มี progress |
| **Project I/O** | Save/Load เป็น JSON (`project.json` ไฟล์เดียว) |
| **Tests** | Unit test สำหรับ split, move, total_duration |

### ข้อจำกัดหลักที่พบ
- ไม่มี Undo/Redo
- ไม่มี keyboard shortcuts
- Export blocking ไม่มี progress bar
- ไม่มี trim in/out แบบ manual
- ไฟล์ไม่มี audio stream → export crash
- ไม่มี transition/effect ใดๆ
- Export hardcode resolution/codec
- Timeline แสดงแค่ชื่อไฟล์ ไม่มี thumbnail/waveform
- Save ได้แค่ไฟล์เดียว ไม่มี auto-save
- รองรับแค่ track เดียว (V1/A1)

---

## ฟีเจอร์ #1 — Undo / Redo

### ปัญหา
ผู้ใช้ split/ลบคลิปผิดแล้วย้อนกลับไม่ได้ ต้อง Load project.json ใหม่เท่านั้น

### แนวทาง
- สร้าง `UndoManager` class เก็บ history stack ของ `Project` state (deep copy)
- จำกัด history ไว้ ~50 steps เพื่อไม่กิน RAM มากเกินไป
- ทุกครั้งที่ clips เปลี่ยน (`split_click`, `delete_click`, drag-drop) → push state ก่อนเปลี่ยน

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/undo.py` | สร้างใหม่ — `UndoManager` class มี `push()`, `undo()`, `redo()` |
| `app.py` | เรียก `undo_mgr.push()` ก่อนทุก action ที่เปลี่ยน clips, เพิ่มปุ่ม Undo/Redo ใน toolbar |

### โค้ดตัวอย่าง (core/undo.py)
```python
from __future__ import annotations
import copy
from typing import List, Optional
from .model import Project

class UndoManager:
    def __init__(self, max_history: int = 50) -> None:
        self._history: List[Project] = []
        self._future: List[Project] = []
        self._max = max_history

    def push(self, state: Project) -> None:
        self._history.append(copy.deepcopy(state))
        if len(self._history) > self._max:
            self._history.pop(0)
        self._future.clear()

    def undo(self, current: Project) -> Optional[Project]:
        if not self._history:
            return None
        self._future.append(copy.deepcopy(current))
        return self._history.pop()

    def redo(self, current: Project) -> Optional[Project]:
        if not self._future:
            return None
        self._history.append(copy.deepcopy(current))
        return self._future.pop()

    @property
    def can_undo(self) -> bool:
        return len(self._history) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._future) > 0
```

### ความยาก: กลาง | ผลกระทบ: สูง

---

## ฟีเจอร์ #2 — Keyboard Shortcuts

### ปัญหา
ทุก action ต้องคลิกปุ่ม ทำให้ workflow ช้า ตัดต่อไม่ลื่น

### แนวทาง
- ใช้ `page.on_keyboard_event` ของ Flet เพื่อดักจับ keyboard events
- Map ปุ่มเข้ากับ action ที่มีอยู่แล้ว

### Shortcut ที่ควรมี
| ปุ่ม | Action |
|---|---|
| `Delete` / `Backspace` | ลบคลิปที่เลือก |
| `S` | Split คลิปที่เลือก |
| `Ctrl+S` | Save project |
| `Ctrl+Z` | Undo |
| `Ctrl+Y` / `Ctrl+Shift+Z` | Redo |
| `Ctrl+E` | Export |
| `Ctrl+I` | Import files |
| `+` / `-` | Zoom in/out timeline |
| `←` / `→` | เลือกคลิปก่อน/หลัง |

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `app.py` | เพิ่ม `page.on_keyboard_event = on_keyboard` handler ที่ dispatch ไปยัง function ที่มีอยู่แล้ว |

### โค้ดตัวอย่าง
```python
def on_keyboard(e: ft.KeyboardEvent) -> None:
    k = e.key
    ctrl = e.ctrl
    if k == "Delete" or k == "Backspace":
        delete_click(None)
    elif k == "S" and ctrl:
        save_click(None)
    elif k == "S" and not ctrl:
        split_click(None)
    elif k == "Z" and ctrl:
        undo_click(None)
    elif k == "Y" and ctrl:
        redo_click(None)
    elif k == "E" and ctrl:
        export_click(None)
    elif k == "I" and ctrl:
        import_click(None)
    elif k == "+" or k == "=":
        timeline_zoom.value = min(180, timeline_zoom.value + 10)
        on_zoom(None)
    elif k == "-":
        timeline_zoom.value = max(20, timeline_zoom.value - 10)
        on_zoom(None)

page.on_keyboard_event = on_keyboard
```

### ความยาก: ง่าย | ผลกระทบ: สูง

---

## ฟีเจอร์ #3 — Export Progress Bar

### ปัญหา
`export_timeline()` ใช้ `subprocess.run()` แบบ blocking — UI ค้างจนกว่า FFmpeg จะเสร็จ ผู้ใช้ไม่รู้ว่าอยู่ตรงไหน อาจปิดโปรแกรมเพราะคิดว่าแฮงค์

### แนวทาง
- เปลี่ยนจาก `subprocess.run()` → `subprocess.Popen()` + อ่าน `stderr` แบบ realtime
- FFmpeg พิมพ์ progress ลง stderr (มี `time=...` ในแต่ละบรรทัด)
- Parse เวลาปัจจุบัน เทียบกับ `total_duration` → คำนวณ %
- แสดง `ft.ProgressBar` + label บน UI
- รันใน thread แยกเพื่อไม่ block UI

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/ffmpeg.py` | เพิ่ม `export_timeline_async()` ที่ yield progress |
| `app.py` | เพิ่ม progress dialog/bar, เรียก export ใน background thread |

### โค้ดตัวอย่าง (core/ffmpeg.py)
```python
import re
from typing import Generator, Tuple

def export_timeline_with_progress(
    ffmpeg_path: str, clips: List[Clip], out_path: str
) -> Generator[Tuple[float, float], None, None]:
    """Yield (current_sec, total_sec) as FFmpeg encodes."""
    total = sum(c.dur for c in clips)
    cmd = build_export_command(ffmpeg_path, clips, out_path)
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)

    time_re = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
    for line in proc.stderr:
        m = time_re.search(line)
        if m:
            h, mi, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            current = h * 3600 + mi * 60 + s
            yield current, total

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg exited with code {proc.returncode}")
    yield total, total
```

### ความยาก: กลาง | ผลกระทบ: สูง

---

## ฟีเจอร์ #4 — Trim In/Out แบบ Manual

### ปัญหา
ตอนนี้ทำได้แค่ Split — ไม่สามารถลากหรือพิมพ์ค่า in/out point ของคลิปเพื่อ trim ได้ ต้อง split แล้วลบส่วนที่ไม่ต้องการ (2 ขั้นตอน)

### แนวทาง
- เพิ่ม Slider คู่ (range slider) หรือ TextField 2 ช่องใน Inspector สำหรับ `in_sec` / `out_sec`
- เมื่อค่าเปลี่ยน → update clip object ใน `state.project.clips` (non-destructive)
- Validate: `in_sec >= 0`, `out_sec <= source_duration`, `out_sec > in_sec + min_dur`

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `app.py` (Inspector section) | เพิ่ม in/out slider หรือ TextField + ปุ่ม Apply Trim |
| `core/timeline.py` | เพิ่ม `trim_clip(clips, clip_id, new_in, new_out)` function |
| `core/model.py` | อาจเพิ่ม `source_duration` field ใน Clip เพื่อรู้ขอบเขตเดิม |

### โค้ดตัวอย่าง (core/timeline.py)
```python
def trim_clip(
    clips: List[Clip], clip_id: str, new_in: float, new_out: float
) -> Tuple[List[Clip], str]:
    """Adjust in/out of a clip. Returns (new_clips, message)."""
    if new_in < 0 or new_out <= new_in:
        return clips, "ค่า in/out ไม่ถูกต้อง"
    out: List[Clip] = []
    for c in clips:
        if c.id == clip_id:
            out.append(replace(c, in_sec=new_in, out_sec=new_out))
        else:
            out.append(c)
    return out, f"Trim แล้ว ({_fmt(new_in)} → {_fmt(new_out)})"
```

### ความยาก: ง่าย | ผลกระทบ: สูง

---

## ฟีเจอร์ #5 — รองรับไฟล์ที่ไม่มี Audio Stream

### ปัญหา
`build_export_command()` สมมติว่าทุกไฟล์มีทั้ง video+audio — ถ้าไฟล์ใดไม่มี audio (เช่น GIF ที่แปลงเป็น mp4, screen recording ไม่มีเสียง) FFmpeg จะ error: `Stream specifier ':a' ... matches no streams`

### แนวทาง
- เก็บ `has_audio` ไว้ใน `Clip` model (ได้จาก `probe_media()` ตอน import)
- ใน `build_export_command()` ถ้าคลิปไม่มี audio → ใช้ `anullsrc` filter สร้าง silent audio ให้
- ทำให้ concat ทำงานได้ทุกกรณี

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/model.py` | เพิ่ม field `has_audio: bool = True` ใน `Clip` dataclass |
| `core/ffmpeg.py` | แก้ `build_export_command()` — ถ้า `clip.has_audio == False` ใช้ `anullsrc` แทน `atrim` |
| `app.py` | ส่ง `has_audio` จาก `probe_media()` มาเก็บตอนสร้าง clip |

### โค้ดตัวอย่าง (filter สำหรับ clip ไม่มี audio)
```python
if c.has_audio:
    parts.append(
        f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS[{a}]"
    )
else:
    parts.append(
        f"anullsrc=r=44100:cl=stereo[{a}_raw];[{a}_raw]atrim=0:{c.dur},asetpts=PTS-STARTPTS[{a}]"
    )
```

### ความยาก: ง่าย | ผลกระทบ: กลาง (แก้ bug สำคัญ)

---

## ฟีเจอร์ #6 — Transition / Fade Effects

### ปัญหา
คลิปต่อกันแบบ hard cut เท่านั้น ไม่มี transition ให้เลือก ทำให้วิดีโอดูกระตุก

### แนวทาง
- เพิ่ม `Transition` model (type: fade/crossfade/dissolve, duration: float)
- เก็บเป็น property ระหว่างคลิป (เช่น `clip.transition_in`)
- แก้ `build_export_command()` ให้ใส่ `xfade` (video) + `acrossfade` (audio) filter

### ไฟล์ที่ต้องแก้/สร้าง
| ไฟล์ | รายละเอียด |
|---|---|
| `core/model.py` | เพิ่ม `Transition` dataclass, เพิ่ม field `transition_in` ใน `Clip` |
| `core/ffmpeg.py` | แก้ filter_complex ให้รองรับ `xfade` + `acrossfade` |
| `app.py` | เพิ่ม Transition picker ใน Inspector (dropdown: None/Fade/Dissolve + duration slider) |
| `core/timeline.py` | ปรับ `total_duration()` ให้หัก overlap จาก transition |

### FFmpeg filter ตัวอย่าง
```
[v0][v1]xfade=transition=fade:duration=0.5:offset=4.5[vx01]
[a0][a1]acrossfade=d=0.5[ax01]
```

### ความยาก: ยาก | ผลกระทบ: กลาง

---

## ฟีเจอร์ #7 — Export Settings (Resolution / Quality / Format)

### ปัญหา
Hardcode เป็น libx264 + aac 192k ตลอด ไม่สามารถเลือก resolution, quality, หรือ format ได้

### แนวทาง
- สร้าง `ExportSettings` dataclass
- แสดง dialog ก่อน export ให้เลือกค่า
- ส่ง settings ไปยัง `build_export_command()`

### ExportSettings Model
```python
@dataclass
class ExportSettings:
    width: int = 1920          # 0 = keep original
    height: int = 1080
    video_codec: str = "libx264"
    crf: int = 23              # 0-51, lower = better quality
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    format: str = "mp4"        # mp4, webm, mov
    preset: str = "medium"     # ultrafast → veryslow
```

### Preset ที่ควรมี
| Preset | Resolution | CRF | ใช้งาน |
|---|---|---|---|
| Social (IG/TikTok) | 1080x1920 | 20 | แนวตั้ง |
| YouTube 1080p | 1920x1080 | 20 | มาตรฐาน |
| YouTube 720p | 1280x720 | 23 | ไฟล์เล็ก |
| Draft (เร็ว) | 854x480 | 28 | ดูตัวอย่าง |
| Custom | ผู้ใช้กำหนด | ผู้ใช้กำหนด | — |

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/model.py` | เพิ่ม `ExportSettings` dataclass |
| `core/ffmpeg.py` | `build_export_command()` รับ `ExportSettings` เพิ่ม `-vf scale`, `-crf`, `-preset` |
| `app.py` | เพิ่ม Export Settings dialog (dropdown + sliders) |

### ความยาก: กลาง | ผลกระทบ: กลาง

---

## ฟีเจอร์ #8 — Thumbnail / Waveform บน Timeline

### ปัญหา
คลิปบน Timeline แสดงแค่ชื่อไฟล์ + สี → หา scene ที่ต้องการยาก โดยเฉพาะเมื่อมีหลายคลิปจากไฟล์เดียวกัน

### แนวทาง
- **Thumbnail**: ใช้ FFmpeg สร้าง thumbnail จาก frame แรก (หรือ in_sec) ของแต่ละคลิป เก็บเป็น temp file
- **Waveform**: ใช้ FFmpeg สร้าง waveform image จาก audio stream
- Cache ไว้ใน temp folder ตาม source file hash
- แสดงเป็น background image ของ clip block บน timeline

### FFmpeg command สร้าง thumbnail
```bash
ffmpeg -ss {in_sec} -i {src} -frames:v 1 -vf "scale=160:-1" -y {thumb_path}
```

### FFmpeg command สร้าง waveform
```bash
ffmpeg -i {src} -filter_complex "showwavespic=s=320x40:colors=white" -frames:v 1 -y {wave_path}
```

### ไฟล์ที่ต้องแก้/สร้าง
| ไฟล์ | รายละเอียด |
|---|---|
| `core/thumbnails.py` | สร้างใหม่ — `generate_thumbnail()`, `generate_waveform()` + cache logic |
| `app.py` | แก้ `clip_block()` ให้โหลด thumbnail เป็น background image |

### ความยาก: ยาก | ผลกระทบ: กลาง

---

## ฟีเจอร์ #9 — Auto-Save + Recent Projects

### ปัญหา
- Save ได้แค่ path เดียว (`project.json` ที่ root)
- ไม่มี auto-save → ปิดโปรแกรมแล้วงานหาย
- ไม่มีรายชื่อโปรเจกต์ล่าสุด → เปิดงานเก่ายาก

### แนวทาง
- **Save As**: เพิ่ม FilePicker สำหรับ save project ไปที่ไหนก็ได้
- **Auto-save**: ตั้ง timer ทุก 60 วินาที save ไปยัง path ปัจจุบัน (ถ้ามี)
- **Recent Projects**: เก็บ list ของ path ที่เคยเปิดไว้ในไฟล์ config (`~/.minicut/recent.json`)
- แสดงรายชื่อ recent projects ตอนเปิดเปล่าๆ หรือในเมนู

### ไฟล์ที่ต้องแก้/สร้าง
| ไฟล์ | รายละเอียด |
|---|---|
| `core/config.py` | สร้างใหม่ — จัดการ config/recent projects |
| `core/project_io.py` | เพิ่ม `auto_save()`, ปรับให้รองรับ path ที่กำหนดเอง |
| `app.py` | เพิ่ม Save As button, timer auto-save, recent projects list |

### Config structure
```json
{
  "recent": [
    {"path": "C:/Users/.../project1.json", "name": "งานวิดีโอ 1", "last_opened": "2026-02-09T10:30:00"},
    {"path": "D:/Projects/wedding.json", "name": "wedding", "last_opened": "2026-02-08T14:20:00"}
  ],
  "auto_save_interval_sec": 60,
  "last_export_dir": "C:/Users/.../Videos"
}
```

### ความยาก: ง่าย | ผลกระทบ: กลาง

---

## ฟีเจอร์ #10 — Multiple Tracks (V2, V3, A2...)

### ปัญหา
ตอนนี้เป็น linear timeline เส้นเดียว (V1/A1) — ไม่สามารถ overlay text, วางรูปซ้อน (PIP), หรือ mix เสียงหลายชั้นได้

### แนวทาง (long-term)
- ปรับ `Project` model ให้มี `tracks: List[Track]` แทน `clips: List[Clip]`
- แต่ละ `Track` มี type (video/audio), ชื่อ, และ list ของ clips
- Timeline UI แสดงหลายแถว แต่ละแถวเป็น track
- Export ต้องสร้าง `overlay` / `amix` filter chain ตามจำนวน track

### Data Model ใหม่
```python
@dataclass
class Track:
    id: str
    name: str
    kind: str  # "video" | "audio"
    clips: List[Clip]
    muted: bool = False
    visible: bool = True

@dataclass
class Project:
    tracks: List[Track]
    fps: int = 30

    @property
    def video_tracks(self) -> List[Track]:
        return [t for t in self.tracks if t.kind == "video"]

    @property
    def audio_tracks(self) -> List[Track]:
        return [t for t in self.tracks if t.kind == "audio"]
```

### ผลกระทบต่อโค้ดที่มีอยู่
| ส่วน | ต้องแก้ |
|---|---|
| `core/model.py` | เพิ่ม `Track`, ปรับ `Project` |
| `core/timeline.py` | ทุก function ต้องรับ `track_id` เพิ่ม |
| `core/ffmpeg.py` | `build_export_command()` ต้องสร้าง overlay/amix chain |
| `core/project_io.py` | ปรับ serialization format |
| `app.py` | Timeline UI แสดงหลาย row, เพิ่มปุ่ม Add/Remove Track |
| `tests/` | ปรับ test ทุกตัว |

### ⚠️ Breaking change — ต้อง migrate project.json format เดิม

### ความยาก: ยากมาก | ผลกระทบ: สูง (long-term)

---

## Roadmap แนะนำ

### Phase 1 — Quick Wins (1-2 สัปดาห์)
ฟีเจอร์ที่ง่ายแต่ส่งผลต่อ UX สูง

```
[x] #2 Keyboard Shortcuts          (ง่าย / สูง)
[x] #4 Trim In/Out Manual          (ง่าย / สูง)
[x] #5 รองรับไฟล์ไม่มี Audio       (ง่าย / กลาง — แก้ bug)
[x] #9 Auto-Save + Recent          (ง่าย / กลาง)
```

### Phase 2 — Core UX (2-3 สัปดาห์)
ฟีเจอร์ที่ทำให้โปรแกรมใช้งานได้จริง

```
[ ] #1 Undo/Redo                   (กลาง / สูง)
[ ] #3 Export Progress Bar          (กลาง / สูง)
[ ] #7 Export Settings              (กลาง / กลาง)
```

### Phase 3 — Polish (3-4 สัปดาห์)
ฟีเจอร์เสริมให้โปรแกรมดูดีขึ้น

```
[ ] #6 Transitions                  (ยาก / กลาง)
[ ] #8 Thumbnail/Waveform           (ยาก / กลาง)
```

### Phase 4 — Architecture (4+ สัปดาห์)
ปรับโครงสร้างใหญ่เปิดทางอนาคต

```
[ ] #10 Multiple Tracks             (ยากมาก / สูง long-term)
```

---

## สถาปัตยกรรมไฟล์หลังเพิ่มทุกฟีเจอร์

```
app.py                      ← UI หลัก + keyboard handler
core/
    __init__.py
    model.py                 ← Clip, Track, Project, ExportSettings, Transition
    timeline.py              ← add, split, move, trim, total_duration
    ffmpeg.py                ← probe, build_export_cmd, export_with_progress
    project_io.py            ← save/load/auto-save + migration
    undo.py                  ← UndoManager
    thumbnails.py            ← thumbnail + waveform generation + cache
    config.py                ← app config, recent projects
tests/
    test_timeline.py
    test_undo.py
    test_ffmpeg.py
    test_project_io.py
bin/
    ffmpeg.exe
    ffprobe.exe
```

---

## หมายเหตุ
- ทุกฟีเจอร์ออกแบบให้เป็น **non-destructive editing** (ไม่แก้ไฟล์ต้นฉบับ)
- Priority อาจปรับได้ตาม feedback ของผู้ใช้
- ฟีเจอร์ #10 (Multiple Tracks) เป็น breaking change ควรทำเป็น major version (v2.0)
