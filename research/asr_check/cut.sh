#!/bin/sh
# Вырезает фрагмент записи для быстрой проверки распознавания (004 §12).
# Только звук, моно, ogg/opus — как голосовое Telegram. Запускается на хосте (нужен ffmpeg).
#
#   research/asr_check/cut.sh ЗАПИСЬ НАЧАЛО ИМЯ [СЕКУНД]
#   research/asr_check/cut.sh ~/записи/собес.m4a 00:12:30 answer1
#
# Результат — research/data/asr_check/ИМЯ.ogg. Эта папка в git не попадает: записи — персональные данные.
set -eu
[ $# -ge 3 ] || { sed -n '4,6p' "$0"; exit 2; }
out_dir="$(dirname "$0")/../data/asr_check"
mkdir -p "$out_dir"
ffmpeg -hide_banner -loglevel error -y -ss "$2" -t "${4:-60}" -i "$1" \
  -vn -ac 1 -ar 48000 -c:a libopus -b:a 32k "$out_dir/$3.ogg"
echo "$out_dir/$3.ogg"
