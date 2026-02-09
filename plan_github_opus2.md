# MiniCut — แผนพัฒนา 10 ฟีเจอร์เพิ่มเติม (Opus Plan 2)

> ต่อจาก `plan_github_opus.md` (ฟีเจอร์ #1–#10)  
> สร้างจากการวิเคราะห์โค้ดทั้งโปรเจกต์ เมื่อ 2026-02-09  
> โปรเจกต์: **MiniCut (Flet)** — MVP ตัดต่อวิดีโอแบบเบาๆ คล้าย CapCut  
> Stack: Python + Flet 0.80.5 + flet-video/flet-audio + FFmpeg (subprocess)

---

## ฟีเจอร์ #11 — Text / Title Overlay

### ปัญหา
ไม่สามารถเพิ่มข้อความซ้อนบนวิดีโอได้เลย (ชื่อเรื่อง, credit, caption) — ต้องไปใช้โปรแกรมอื่นเพิ่มทีหลัง

### แนวทาง
- สร้าง `TextOverlay` model เก็บข้อความ, ตำแหน่ง, font, สี, ขนาด, ช่วงเวลาที่แสดง
- เพิ่ม panel "Add Text" ใน UI ให้ผู้ใช้พิมพ์ข้อความ + ปรับ style
- ใช้ FFmpeg `drawtext` filter ตอน export

### Data Model
```python
@dataclass
class TextOverlay:
    id: str
    text: str
    x: str = "center"          # "center", "left", "right" หรือ pixel value
    y: str = "center"          # "center", "top", "bottom" หรือ pixel value
    font_size: int = 48
    font_color: str = "white"
    bg_color: str = ""         # "" = ไม่มีพื้นหลัง, "black@0.5" = ดำโปร่ง 50%
    start_sec: float = 0.0
    end_sec: float = 5.0
    font_file: str = ""        # path ไปยัง .ttf (ว่าง = default)
```

### FFmpeg filter ตัวอย่าง
```
drawtext=text='สวัสดี':fontsize=48:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,0,5)'
```

### รองรับหลายข้อความซ้อนกัน
```
drawtext=text='Title':...:enable='between(t,0,3)',
drawtext=text='Subtitle':...:enable='between(t,1,5)'
```

### ไฟล์ที่ต้องแก้/สร้าง
| ไฟล์ | รายละเอียด |
|---|---|
| `core/model.py` | เพิ่ม `TextOverlay` dataclass, เพิ่ม `overlays: List[TextOverlay]` ใน `Project` |
| `core/ffmpeg.py` | แก้ `build_export_command()` ใส่ `drawtext` filter chain |
| `app.py` | เพิ่ม "Add Text" panel — TextField, color picker, position dropdown, time range |
| `core/project_io.py` | ปรับ save/load ให้รองรับ overlays |

### UI Wireframe
```
┌─── Text Overlay ───────────────────┐
│ ข้อความ: [________________]        │
│ ขนาด:    [48] ▼  สี: [⬜ white]   │
│ ตำแหน่ง:  [Center ▼]              │
│ แสดง:    [00:02.00] → [00:07.00]  │
│ [+ เพิ่มข้อความ]  [🗑 ลบ]          │
└────────────────────────────────────┘
```

### ความยาก: กลาง | ผลกระทบ: สูง

---

## ฟีเจอร์ #12 — Speed Control (เร่ง / สโลว์โมชัน)

### ปัญหา
ตอนนี้คลิปเล่นด้วยความเร็วปกติ (1x) เท่านั้น — ไม่สามารถทำ slow motion หรือ fast forward ได้

### แนวทาง
- เพิ่ม `speed` field ใน `Clip` model (ค่า default = 1.0)
- ใน Inspector เพิ่ม slider ปรับ speed (0.25x → 4.0x)
- แก้ `build_export_command()` ใส่ `setpts` (video) + `atempo` (audio) filter
- ปรับ `dur` property ให้คำนวณตาม speed

### ผลต่อ Duration
```
actual_dur = (out_sec - in_sec) / speed
```
- speed=0.5 → วิดีโอช้าลง 2 เท่า, duration x2
- speed=2.0 → วิดีโอเร็วขึ้น 2 เท่า, duration /2

### FFmpeg filter
```python
# Video: PTS / speed
f"[{idx}:v]trim=start={c.in_sec}:end={c.out_sec},setpts=(PTS-STARTPTS)/{c.speed}[{v}]"

# Audio: atempo (จำกัด 0.5–2.0 ต่อ filter ต้อง chain ถ้าเกิน)
def atempo_chain(speed: float) -> str:
    """atempo รองรับเฉพาะ 0.5-2.0 ต้อง chain ถ้าเกิน"""
    parts = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.4f}")
    return ",".join(parts)
```

### ตัวอย่าง Speed Presets
| Label | Speed | ใช้งาน |
|---|---|---|
| 0.25x | 0.25 | Super Slow Motion |
| 0.5x | 0.5 | Slow Motion |
| 1x | 1.0 | ปกติ |
| 1.5x | 1.5 | เร่งเล็กน้อย |
| 2x | 2.0 | Fast Forward |
| 4x | 4.0 | Timelapse |

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/model.py` | เพิ่ม `speed: float = 1.0` ใน `Clip`, ปรับ `dur` property |
| `core/ffmpeg.py` | แก้ filter ให้ใส่ `setpts` + `atempo` chain |
| `app.py` | เพิ่ม Speed slider ใน Inspector (0.25–4.0, step 0.25) |
| `core/timeline.py` | ปรับ `total_duration()` ให้คำนวณตาม speed |

### ความยาก: กลาง | ผลกระทบ: สูง

---

## ฟีเจอร์ #13 — Audio Volume Control ต่อ Clip

### ปัญหา
ไม่สามารถปรับระดับเสียงของแต่ละคลิปแยกกันได้ ถ้าคลิปหนึ่งเสียงดังมากอีกคลิปเบา ผู้ใช้แก้ไม่ได้ และไม่มี mute

### แนวทาง
- เพิ่ม `volume` (0.0–3.0, default=1.0) และ `muted` (bool) ใน `Clip` model
- ใน Inspector เพิ่ม volume slider + mute checkbox
- ใน `build_export_command()` ใส่ `volume` filter หลัง `atrim`

### FFmpeg filter
```python
# volume=1.0 = ปกติ, 0.5 = เบาลงครึ่งหนึ่ง, 2.0 = ดังเป็น 2 เท่า
if c.muted:
    # ใช้ anullsrc แทน audio จริง
    parts.append(f"anullsrc=r=44100:cl=stereo[{a}_raw];[{a}_raw]atrim=0:{c.dur},asetpts=PTS-STARTPTS[{a}]")
else:
    parts.append(
        f"[{idx}:a]atrim=start={c.in_sec}:end={c.out_sec},asetpts=PTS-STARTPTS,"
        f"volume={c.volume:.2f}[{a}]"
    )
```

### UI ใน Inspector
```
┌─── Audio ──────────────────────┐
│ 🔊 Volume: [====●=====] 1.0x  │
│ ☐ Mute                        │
└────────────────────────────────┘
```

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/model.py` | เพิ่ม `volume: float = 1.0`, `muted: bool = False` ใน `Clip` |
| `core/ffmpeg.py` | ใส่ `volume` filter ใน audio chain |
| `app.py` | เพิ่ม Volume slider + Mute checkbox ใน Inspector |

### ความยาก: ง่าย | ผลกระทบ: สูง

---

## ฟีเจอร์ #14 — Playhead & Scrubber บน Timeline

### ปัญหา
Timeline ไม่มี playhead (เส้นแสดงตำแหน่งเวลาปัจจุบัน) — ผู้ใช้ไม่รู้ว่ากำลังดูตรงไหนของวิดีโอ และไม่สามารถ scrub (ลาก) เพื่อเลื่อนไปยังตำแหน่งที่ต้องการได้

### แนวทาง
- เพิ่ม `playhead_sec` ใน `AppState` (ตำแหน่งเวลาปัจจุบัน)
- วาดเส้นแนวตั้งสีแดงบน Timeline ที่ตำแหน่ง `playhead_sec * px_per_sec`
- เพิ่ม time ruler (แถบเวลา) ด้านบน Timeline
- ให้คลิกบน ruler เพื่อเลื่อน playhead
- Playhead ใช้ร่วมกับ preview player (ถ้ามี) เพื่อ seek ไปยังเวลาที่ต้องการ

### Time Ruler
```
|0:00  |0:05  |0:10  |0:15  |0:20  |0:25  |0:30
  ▼ (playhead)
[====clip1====][==clip2==][=====clip3=====]
[====audio1===][==audio2=][=====audio3====]
```

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `app.py` | เพิ่ม `playhead_sec` state, วาด ruler row + playhead line ใน `refresh_timeline()`, click handler สำหรับ seek |

### โค้ดตัวอย่าง (Ruler + Playhead)
```python
def build_ruler(total_sec: float, px_per_sec: float) -> ft.Row:
    """สร้างแถบเวลาด้านบน Timeline"""
    controls = []
    step = 5  # ทุก 5 วินาที
    for t in range(0, int(total_sec) + step, step):
        controls.append(
            ft.Container(
                width=step * px_per_sec,
                content=ft.Text(f"{t // 60}:{t % 60:02d}", size=10, color=ft.Colors.WHITE54),
                border=ft.Border(left=ft.BorderSide(1, ft.Colors.WHITE24)),
            )
        )
    return ft.Row(controls, spacing=0, scroll=ft.ScrollMode.AUTO)

# Playhead line
playhead_line = ft.Container(
    width=2,
    height=100,
    bgcolor=ft.Colors.RED,
    left=state.playhead_sec * state.px_per_sec,
)
```

### Split ที่ Playhead
เมื่อมี playhead แล้ว สามารถเปลี่ยน Split จาก "slider ใน Inspector" → "split ที่ตำแหน่ง playhead" ซึ่งเป็นวิธีที่ editor ส่วนใหญ่ใช้

### ความยาก: กลาง | ผลกระทบ: สูง

---

## ฟีเจอร์ #15 — Duplicate Clip

### ปัญหา
ถ้าต้องการใช้คลิปเดิมซ้ำหลายครั้ง (เช่น ฉากที่ต้องการวนซ้ำ, intro/outro) ต้องลาก media ลงมาใหม่แล้ว trim ใหม่ทุกครั้ง — ไม่มีปุ่ม Duplicate

### แนวทาง
- เพิ่มปุ่ม "Duplicate" ใน toolbar
- สร้าง copy ของ clip ที่เลือก (new id, เหมือนกันทุกอย่างอื่น)
- แทรกหลังคลิปต้นฉบับ
- รองรับ keyboard shortcut `Ctrl+D`

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/timeline.py` | เพิ่ม `duplicate_clip(clips, clip_id)` function |
| `app.py` | เพิ่มปุ่ม Duplicate + shortcut Ctrl+D |

### โค้ดตัวอย่าง (core/timeline.py)
```python
def duplicate_clip(clips: List[Clip], clip_id: str) -> Tuple[List[Clip], Optional[str], str]:
    """Duplicate a clip and insert it right after the original."""
    out: List[Clip] = []
    new_id_val: Optional[str] = None
    for c in clips:
        out.append(c)
        if c.id == clip_id:
            dup = replace(c, id=new_id())
            out.append(dup)
            new_id_val = dup.id
    if new_id_val is None:
        return clips, None, "ไม่พบคลิปที่จะ Duplicate"
    return out, new_id_val, "Duplicate แล้ว"
```

### ความยาก: ง่ายมาก | ผลกระทบ: กลาง

---

## ฟีเจอร์ #16 — Color Filter / LUT

### ปัญหา
ไม่มีการปรับสีใดๆ — วิดีโอ export ออกมาเหมือนต้นฉบับ 100% ไม่สามารถปรับ brightness, contrast, saturation หรือใส่ filter สีได้

### แนวทาง

#### Level 1: Basic Adjustments (ง่าย)
เพิ่ม slider ปรับค่าสีพื้นฐานต่อคลิป:
- **Brightness** (-1.0 → 1.0, default=0)
- **Contrast** (0.0 → 3.0, default=1.0)
- **Saturation** (0.0 → 3.0, default=1.0)

#### Level 2: Preset Filters (กลาง)
Filter สำเร็จรูปเหมือน Instagram:
- Warm, Cool, Vintage, B&W, High Contrast, Cinematic

#### Level 3: LUT Support (ยาก)
- รองรับไฟล์ `.cube` LUT (3D Look-Up Table)
- FFmpeg: `lut3d=file=my_lut.cube`

### Data Model
```python
@dataclass
class ColorAdjust:
    brightness: float = 0.0     # -1.0 → 1.0
    contrast: float = 1.0       # 0.0 → 3.0
    saturation: float = 1.0     # 0.0 → 3.0
    lut_file: str = ""          # path to .cube file
    preset: str = ""            # "warm", "cool", "vintage", "bw", "cinematic"
```

### FFmpeg filter
```python
# Basic adjustments
f"eq=brightness={adj.brightness}:contrast={adj.contrast}:saturation={adj.saturation}"

# LUT
f"lut3d=file='{adj.lut_file}'"

# Preset: B&W
"hue=s=0"

# Preset: Warm
"colortemperature=temperature=6500"

# Preset: Vintage
"curves=vintage"
```

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/model.py` | เพิ่ม `ColorAdjust` dataclass, เพิ่ม field `color: ColorAdjust` ใน `Clip` |
| `core/ffmpeg.py` | ใส่ `eq` / `lut3d` filter ใน video chain |
| `app.py` | เพิ่ม Color panel ใน Inspector — sliders + preset dropdown |

### UI Wireframe
```
┌─── Color ──────────────────────────┐
│ Preset: [None ▼]                   │
│ ☀ Brightness: [====●=====]  0.0    │
│ ◐ Contrast:   [====●=====]  1.0    │
│ 🎨 Saturation: [====●=====]  1.0   │
│ LUT: [ไม่มี]  [เลือกไฟล์ .cube]    │
└────────────────────────────────────┘
```

### ความยาก: กลาง–ยาก | ผลกระทบ: สูง

---

## ฟีเจอร์ #17 — Subtitle / SRT Import

### ปัญหา
ไม่สามารถเพิ่ม subtitle ได้ — video content ยุคปัจจุบันเกือบทุกแพลตฟอร์มต้องมี subtitle (accessibility + engagement)

### แนวทาง
- รองรับ import ไฟล์ `.srt` (SubRip) — format ที่นิยมที่สุด
- Parse SRT → list ของ `SubtitleEntry`
- แสดง subtitle entries เป็น block ชุดเล็กๆ บน timeline (track แยก)
- ตอน export ใช้ FFmpeg `subtitles` filter หรือ `drawtext` จาก parsed data

### SRT Format
```
1
00:00:01,000 --> 00:00:04,000
สวัสดีครับ ยินดีต้อนรับ

2
00:00:05,500 --> 00:00:08,200
วันนี้เราจะมาสอน MiniCut
```

### Data Model
```python
@dataclass
class SubtitleEntry:
    index: int
    start_sec: float
    end_sec: float
    text: str

@dataclass
class SubtitleTrack:
    id: str
    entries: List[SubtitleEntry]
    font_size: int = 24
    font_color: str = "white"
    bg_color: str = "black@0.5"
    position: str = "bottom"     # "top", "center", "bottom"
```

### SRT Parser
```python
import re

def parse_srt(content: str) -> List[SubtitleEntry]:
    entries = []
    blocks = re.split(r'\n\s*\n', content.strip())
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        index = int(lines[0])
        times = re.match(
            r'(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)',
            lines[1]
        )
        if not times:
            continue
        start = (int(times[1])*3600 + int(times[2])*60 +
                 int(times[3]) + int(times[4])/1000)
        end = (int(times[5])*3600 + int(times[6])*60 +
               int(times[7]) + int(times[8])/1000)
        text = '\n'.join(lines[2:])
        entries.append(SubtitleEntry(index=index, start_sec=start, end_sec=end, text=text))
    return entries
```

### FFmpeg filter (burn-in subtitles)
```bash
# วิธี 1: ใช้ไฟล์ SRT โดยตรง (ง่ายสุด)
ffmpeg -i input.mp4 -vf "subtitles=subs.srt:force_style='FontSize=24'" output.mp4

# วิธี 2: drawtext จาก parsed data (ยืดหยุ่นกว่า)
drawtext=text='สวัสดี':enable='between(t,1,4)':fontsize=24:fontcolor=white:...
```

### ไฟล์ที่ต้องแก้/สร้าง
| ไฟล์ | รายละเอียด |
|---|---|
| `core/subtitle.py` | สร้างใหม่ — `parse_srt()`, `SubtitleEntry`, `SubtitleTrack` |
| `core/model.py` | เพิ่ม `subtitles: List[SubtitleTrack]` ใน `Project` |
| `core/ffmpeg.py` | เพิ่ม subtitle filter ตอน export |
| `app.py` | เพิ่มปุ่ม Import SRT, แสดง subtitle blocks บน timeline |

### ความยาก: กลาง | ผลกระทบ: สูง

---

## ฟีเจอร์ #18 — Crop / Aspect Ratio

### ปัญหา
ไม่สามารถปรับ aspect ratio หรือ crop วิดีโอได้ — ปัจจุบันคนทำ content ต้องแปลง 16:9 → 9:16 (TikTok/Reels) หรือ 1:1 (IG Post) บ่อยมาก

### แนวทาง
- เพิ่ม Aspect Ratio dropdown ระดับ project (ใช้กับ export ทั้งหมด)
- เพิ่ม Crop per clip (optional) สำหรับ crop เฉพาะบางคลิป
- ใช้ FFmpeg `crop` + `scale` + `pad` filter

### Preset Aspect Ratios
| Label | Ratio | Resolution (1080) | ใช้งาน |
|---|---|---|---|
| Landscape 16:9 | 16:9 | 1920×1080 | YouTube, ทั่วไป |
| Portrait 9:16 | 9:16 | 1080×1920 | TikTok, Reels, Shorts |
| Square 1:1 | 1:1 | 1080×1080 | Instagram Post |
| Cinema 21:9 | 21:9 | 2560×1080 | Cinematic |
| Classic 4:3 | 4:3 | 1440×1080 | ย้อนยุค |

### FFmpeg filter
```python
# Center crop to 9:16
f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"

# Scale + Pad (letterbox/pillarbox) — ไม่ตัดอะไรทิ้ง
f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"

# Center crop to 1:1
f"crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))/2:(ih-min(iw\\,ih))/2,scale=1080:1080"
```

### Data Model
```python
@dataclass
class CropSettings:
    mode: str = "fit"       # "fit" (pad) | "fill" (crop) | "stretch"
    aspect: str = "16:9"    # "16:9", "9:16", "1:1", "21:9", "4:3", "custom"
    custom_w: int = 0
    custom_h: int = 0
```

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/model.py` | เพิ่ม `CropSettings`, เพิ่มใน `Project` (global) หรือ `Clip` (per-clip) |
| `core/ffmpeg.py` | เพิ่ม crop/scale/pad filter ใน video chain |
| `app.py` | เพิ่ม Aspect Ratio dropdown + Crop mode selector |

### ความยาก: กลาง | ผลกระทบ: สูง

---

## ฟีเจอร์ #19 — Media Info Panel

### ปัญหา
ผู้ใช้ไม่เห็นข้อมูลรายละเอียดของไฟล์ที่ import เข้ามา (resolution, codec, fps, bitrate, file size) — ไม่รู้ว่าไฟล์ต้นทางเป็นอะไร แก้ปัญหา export ยาก

### แนวทาง
- ขยาย `MediaInfo` ที่ได้จาก `probe_media()` ให้เก็บข้อมูลเพิ่ม
- แสดง info panel เมื่อ hover หรือ click ไฟล์ใน Media Bin
- ใช้ข้อมูลนี้ช่วยแนะนำ export settings ด้วย

### ขยาย MediaInfo
```python
@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_video: bool
    has_audio: bool
    # ─── ใหม่ ───
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    video_bitrate: int = 0      # bps
    audio_bitrate: int = 0      # bps
    file_size_bytes: int = 0
    pixel_format: str = ""
    sample_rate: int = 0
    channels: int = 0
```

### วิธีดึงข้อมูลจาก ffprobe
```python
# จาก streams
for s in streams:
    if s.get("codec_type") == "video":
        width = int(s.get("width", 0))
        height = int(s.get("height", 0))
        fps_str = s.get("r_frame_rate", "0/1")
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) else 0
        video_codec = s.get("codec_name", "")
        pixel_format = s.get("pix_fmt", "")
    elif s.get("codec_type") == "audio":
        audio_codec = s.get("codec_name", "")
        sample_rate = int(s.get("sample_rate", 0))
        channels = int(s.get("channels", 0))
```

### UI Wireframe (Tooltip/Panel)
```
┌─── Media Info ─────────────────────┐
│ 📁 vacation.mp4                    │
│ ──────────────────────────────     │
│ Resolution: 1920×1080              │
│ FPS:        29.97                  │
│ Duration:   02:35.40               │
│ Video:      h264 (yuv420p)         │
│ Audio:      aac 48000Hz stereo     │
│ Bitrate:    8.2 Mbps               │
│ File Size:  156.3 MB               │
└────────────────────────────────────┘
```

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `core/ffmpeg.py` | ขยาย `probe_media()` และ `MediaInfo` |
| `core/model.py` | อาจเพิ่ม `MediaItem` หรือเก็บ info ใน `AppState.media` |
| `app.py` | เพิ่ม info display ใน Media Bin (tooltip หรือ expandable panel) |

### ความยาก: ง่าย | ผลกระทบ: กลาง

---

## ฟีเจอร์ #20 — Drag & Drop Import จาก File Explorer

### ปัญหา
ตอนนี้ต้องกดปุ่ม Import → เลือกไฟล์ใน File Picker ทุกครั้ง — ไม่สามารถลากไฟล์จาก Windows Explorer มาวางในแอปได้โดยตรง

### แนวทาง
- ใช้ `page.on_drop` หรือ DragTarget ที่ครอบ Media Bin ทั้งอัน
- Flet 0.80.5 รองรับ `page.on_drop` สำหรับ file drop จาก OS
- เมื่อรับไฟล์ → probe แล้วเพิ่มเข้า Media Bin เหมือน import ปกติ

### โค้ดตัวอย่าง
```python
def on_file_drop(e) -> None:
    """Handle files dropped from OS file manager."""
    if not e.files:
        return
    bins = get_bins()
    if not bins:
        return
    _, ffprobe = bins

    allowed_ext = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".mp3", ".wav", ".flac"}
    for f in e.files:
        ext = Path(f.path).suffix.lower()
        if ext not in allowed_ext:
            snack(f"ไม่รองรับไฟล์ {ext}")
            continue
        try:
            info = probe_media(ffprobe, f.path)
            if info.duration <= 0.01:
                continue
            state.media.append(MediaItem(path=f.path, duration=info.duration))
        except Exception as ex:
            log.exception("probe failed: %s", ex)
            snack(f"อ่านไฟล์ไม่สำเร็จ: {Path(f.path).name}")

    refresh_media()

# ลงทะเบียน handler
page.on_drop = on_file_drop
```

### รองรับ Format เพิ่ม
ตอนนี้รองรับแค่ `mp4`, `mov`, `mkv` — ควรเพิ่ม:

| ประเภท | Extensions |
|---|---|
| Video | `.mp4`, `.mov`, `.mkv`, `.avi`, `.webm`, `.flv`, `.wmv`, `.m4v` |
| Audio only | `.mp3`, `.wav`, `.flac`, `.aac`, `.ogg`, `.m4a` |
| Image sequence | `.jpg`, `.png`, `.bmp` (ทำเป็น still frame clip) |

### ไฟล์ที่ต้องแก้
| ไฟล์ | รายละเอียด |
|---|---|
| `app.py` | เพิ่ม `page.on_drop` handler, ขยาย allowed extensions |

### ความยาก: ง่าย | ผลกระทบ: กลาง

---

## Roadmap รวม (ฟีเจอร์ #11–#20)

### Phase A — Quick Wins (1-2 สัปดาห์)
```
[ ] #13 Audio Volume Control        (ง่าย / สูง)
[ ] #15 Duplicate Clip              (ง่ายมาก / กลาง)
[ ] #19 Media Info Panel            (ง่าย / กลาง)
[ ] #20 Drag & Drop Import          (ง่าย / กลาง)
```

### Phase B — Content Features (2-4 สัปดาห์)
```
[ ] #11 Text/Title Overlay          (กลาง / สูง)
[ ] #12 Speed Control               (กลาง / สูง)
[ ] #14 Playhead & Scrubber         (กลาง / สูง)
[ ] #17 Subtitle/SRT Import         (กลาง / สูง)
```

### Phase C — Pro Features (4-6 สัปดาห์)
```
[ ] #18 Crop / Aspect Ratio         (กลาง / สูง)
[ ] #16 Color Filter / LUT          (กลาง-ยาก / สูง)
```

---

## สรุปทั้ง 20 ฟีเจอร์ (Plan 1 + Plan 2)

| # | ฟีเจอร์ | ความยาก | ผลกระทบ | Phase |
|:---:|---|:---:|:---:|:---:|
| 1 | Undo/Redo | กลาง | สูง | P2 |
| 2 | Keyboard Shortcuts | ง่าย | สูง | P1 |
| 3 | Export Progress Bar | กลาง | สูง | P2 |
| 4 | Trim In/Out Manual | ง่าย | สูง | P1 |
| 5 | รองรับไฟล์ไม่มี Audio | ง่าย | กลาง | P1 |
| 6 | Transitions | ยาก | กลาง | P3 |
| 7 | Export Settings | กลาง | กลาง | P2 |
| 8 | Thumbnail/Waveform | ยาก | กลาง | P3 |
| 9 | Auto-Save + Recent | ง่าย | กลาง | P1 |
| 10 | Multiple Tracks | ยากมาก | สูง | P4 |
| 11 | Text/Title Overlay | กลาง | สูง | B |
| 12 | Speed Control | กลาง | สูง | B |
| 13 | Audio Volume Control | ง่าย | สูง | A |
| 14 | Playhead & Scrubber | กลาง | สูง | B |
| 15 | Duplicate Clip | ง่ายมาก | กลาง | A |
| 16 | Color Filter / LUT | กลาง-ยาก | สูง | C |
| 17 | Subtitle / SRT Import | กลาง | สูง | B |
| 18 | Crop / Aspect Ratio | กลาง | สูง | C |
| 19 | Media Info Panel | ง่าย | กลาง | A |
| 20 | Drag & Drop Import | ง่าย | กลาง | A |

---

## หมายเหตุ
- Phase P1–P4 = จาก Plan 1 (opus.md), Phase A–C = จาก Plan 2 (opus2.md)
- สามารถทำ Plan 1 และ Plan 2 สลับกันได้ตาม priority
- ฟีเจอร์ #11, #12, #14, #17, #18 รวมกันจะทำให้ MiniCut เทียบเคียง CapCut เวอร์ชันเบาได้
- ทุกฟีเจอร์ออกแบบให้เป็น **non-destructive editing** ไม่แก้ไฟล์ต้นฉบับ
