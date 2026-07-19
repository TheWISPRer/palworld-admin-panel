# Map calibration

The panel needs a map image (`map.png`) and four constants that map in-game
coordinates onto that specific image.

**No map image ships with this project.** Palworld's map is Pocketpair's
artwork; redistributing it isn't something this repo does. Supply your own —
a screenshot of the in-game map works fine, as do community-made maps if
their licence permits.

Drop it at `$PANEL_DATA_DIR/map.png` (e.g. `/var/lib/palworld-admin/map.png`).

## Why calibration is needed

In-game coordinates run roughly `-1000..1000` on both axes. The panel converts
a coordinate to a fraction of the image with a simple linear fit:

```
fx = MAP_FX_SLOPE * game_x + MAP_FX_OFFSET     # 0.0 = left edge,   1.0 = right
fy = MAP_FY_SLOPE * game_y + MAP_FY_OFFSET     # 0.0 = top edge,    1.0 = bottom
```

`MAP_FY_SLOPE` is negative because in-game +Y is north (up) while image Y
grows downward.

If your image covers the full world bounds edge to edge, the defaults
(`±0.0005` slope, `0.5` offset) are already correct. They usually aren't:
most map images are cropped, padded, or letterboxed, so players render
offset until you re-fit.

## Fitting it

You need two reference points — the further apart, the better the fit.

1. **Pick two landmarks** you can find both in-game and on your image. Fast
   travel statues and towers work well.
2. **Get in-game coordinates**: stand at each and read the coordinate display
   on the pause-menu map.
3. **Get image fractions**: open `map.png` in any image editor, hover over the
   same landmark, note the pixel position, and divide by the image dimensions.
   A landmark at x=430 in a 1123px-wide image is `430 / 1123 = 0.383`.
4. **Solve** the two linear equations per axis:

```
MAP_FX_SLOPE  = (fx2 - fx1) / (gx2 - gx1)
MAP_FX_OFFSET = fx1 - MAP_FX_SLOPE * gx1

MAP_FY_SLOPE  = (fy2 - fy1) / (gy2 - gy1)
MAP_FY_OFFSET = fy1 - MAP_FY_SLOPE * gy1
```

Or let Python do it:

```python
# (game_coord, image_fraction) for two landmarks, per axis
x1, fx1 = -72, 0.517
x2, fx2 = 513, 0.782
slope = (fx2 - fx1) / (x2 - x1)
print(f"MAP_FX_SLOPE={slope:.8f}")
print(f"MAP_FX_OFFSET={fx1 - slope * x1:.6f}")
```

5. Put the results in `.env`, restart, and check a live player's dot against
   their in-game position. A third landmark is a good sanity check.

```env
MAP_FX_SLOPE=0.00045295
MAP_FX_OFFSET=0.550638
MAP_FY_SLOPE=-0.00055509
MAP_FY_OFFSET=0.481390
```

## Gotchas

- **Replacing `map.png` invalidates the constants.** A different crop, or even
  a different resolution with different padding, needs a re-fit.
- **Coordinate order.** The in-game map displays coordinates as
  *(first, second)* where the first number drives the horizontal axis, but the
  REST API returns them in the opposite order. `raw_to_game()` in `app.py`
  already normalises this — don't "fix" it.
- **Newer islands may be missing** from older map images. Players there will
  render over open water. Only fix is a newer image, plus a re-fit.
