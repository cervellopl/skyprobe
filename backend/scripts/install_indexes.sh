#!/usr/bin/env bash
# Download astrometry.net index files for the field sizes you actually photograph.
#
#   ./install_indexes.sh phone       wide lens / phone main camera   (~8° … 33°+ fields)
#   ./install_indexes.sh telephoto   phone tele lens, camera lenses  (~1° … 8°)
#   ./install_indexes.sh seestar     Seestar S30/S50, small scopes   (~20′ … 2°)
#   ./install_indexes.sh deep        adds the 4200 series for fields below 20′ (several GB)
#
# The 4100 series (Tycho-2/2MASS, one file per scale) is small; the 4200 series is split
# into 48 healpix tiles per scale. Index numbers are scales: 4119 ≈ 2000′, 4110 ≈ 60′,
# 4107 ≈ 22′, 4205 ≈ 11′, 4203 ≈ 5.6′.
#
# On Debian/Ubuntu the same data is available as packages:
#   sudo apt install astrometry.net astrometry-data-tycho2-{07,08,09,10-19}-littleendian
set -euo pipefail

DEST="${DEST:-/usr/share/astrometry}"
B41="http://data.astrometry.net/4100"
B42="http://data.astrometry.net/4200"
PROFILE="${1:-seestar}"

case "$PROFILE" in
  phone)     SCALES_41="$(seq 14 19)"; SCALES_42="" ;;
  telephoto) SCALES_41="$(seq 10 16)"; SCALES_42="" ;;
  seestar)   SCALES_41="07 08 09 10 11 12"; SCALES_42="" ;;
  deep)      SCALES_41="07 08"; SCALES_42="03 04 05 06" ;;
  *) echo "unknown profile: $PROFILE (phone|telephoto|seestar|deep)" >&2; exit 2 ;;
esac

mkdir -p "$DEST"
echo "Downloading index files into $DEST (profile: $PROFILE)"
for s in $SCALES_41; do
  name="index-41$(printf '%02d' "$((10#$s))").fits"
  [ -s "$DEST/$name" ] && { echo "  $name already there"; continue; }
  echo "  $name"
  curl -fL --progress-bar -o "$DEST/$name" "$B41/$name"
done
for s in $SCALES_42; do
  for hp in $(seq -w 0 47); do
    name="index-42$(printf '%02d' "$((10#$s))")-$hp.fits"
    [ -s "$DEST/$name" ] && continue
    curl -fL --progress-bar -o "$DEST/$name" "$B42/$name" || rm -f "$DEST/$name"
  done
done

cfg=/etc/astrometry.cfg
if [ -w "$cfg" ] && ! grep -q "^add_path $DEST" "$cfg" 2>/dev/null; then
  printf '\nadd_path %s\nautoindex\n' "$DEST" >> "$cfg"
  echo "Registered $DEST in $cfg"
fi
echo "Done: $(ls "$DEST" | wc -l) files, $(du -sh "$DEST" | cut -f1) total."
