========================================================
  kq — Brand Assets
========================================================

Files:
  kq-mark.svg         Square mark (dark bg) — use on dark surfaces, social avatars
  kq-mark-light.svg   Square mark (light bg) — use on white/light surfaces, print
  kq-lockup.svg       Horizontal lockup — use in headers, README banners, email signatures

Palette:
  Background dark  #070b17  (deep navy)
  Background card  #0b1020
  Border           #1e2642
  Text primary     #e2e8f0
  Text muted       #8a94b0
  Accent gradient  #818cf8 → #6366f1  (indigo)
  Accent solid     #6366f1

Typography:
  Wordmark  JetBrains Mono, 700
  UI label  Inter 900 (kube-q lockup) / Inter 500 (captions)

Sizing:
  Favicon        32x32, 48x48 from kq-mark.svg
  App icon       256x256 from kq-mark.svg
  Social avatar  400x400 from kq-mark.svg
  GitHub banner  kq-lockup.svg at 720x200

How to export PNG:
  rsvg-convert -w 512 kq-mark.svg > kq-mark-512.png
  rsvg-convert -w 1600 kq-lockup.svg > kq-lockup-1600.png

========================================================
