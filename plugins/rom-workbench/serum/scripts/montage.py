#!/usr/bin/env python3
"""Tile a set of 128x32 PPM frames into one montage PPM (-> sips to PNG).
Usage: montage.py <out.ppm> <cols> <scale> <file1.ppm> [file2.ppm ...]
Prints the grid order (index -> filename) to stderr."""
import sys, numpy as np

def read_ppm(path):
    with open(path, 'rb') as f:
        data = f.read()
    assert data[:2] == b'P6', path
    # parse header: P6 W H MAXVAL\n then binary
    idx = 2
    vals = []
    while len(vals) < 3:
        while idx < len(data) and data[idx] in b' \t\n\r':
            idx += 1
        start = idx
        while idx < len(data) and data[idx] not in b' \t\n\r':
            idx += 1
        vals.append(int(data[start:idx]))
    idx += 1  # single whitespace after maxval
    w, h, mx = vals
    arr = np.frombuffer(data[idx:idx+w*h*3], dtype=np.uint8).reshape(h, w, 3)
    return arr

def main():
    out, cols, scale = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    files = sys.argv[4:]
    gap = 2
    cell_h, cell_w = 32*scale, 128*scale
    rows = (len(files) + cols - 1) // cols
    H = rows*(cell_h+gap) - gap
    W = cols*(cell_w+gap) - gap
    canvas = np.full((H, W, 3), 40, dtype=np.uint8)  # dark gray bg
    for i, fp in enumerate(files):
        r, c = divmod(i, cols)
        img = read_ppm(fp)
        img = np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)
        y, x = r*(cell_h+gap), c*(cell_w+gap)
        canvas[y:y+cell_h, x:x+cell_w] = img
        print(f"{i}\t{fp.split('/')[-1]}", file=sys.stderr)
    with open(out, 'wb') as f:
        f.write(f"P6\n{W} {H}\n255\n".encode())
        f.write(canvas.tobytes())
    print(f"wrote {out} ({W}x{H}, {len(files)} frames, {cols} cols)", file=sys.stderr)

if __name__ == '__main__':
    main()
