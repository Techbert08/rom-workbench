// pac2serum — convert PIN2DMD colorization (.pal + .vni) to Serum V1 (.cROMc)
//
// Usage:
//   pac2serum --pal <file.pal> --vni <file.vni> --rom <romname> --out <file.cROMc>
//
// How it works
// ─────────────
// 1. Parse the PAL file: 170 palettes (64 RGB colours each) and 316 frame
//    mappings (CRC32 → palette index + byte offset into the VNI file).
// 2. Parse the VNI file: 261 animations (each a sequence of bit-plane encoded,
//    optionally heatshrink-compressed 128×32 indexed colour frames).
// 3. For every PAL mapping, look up the first frame of the referenced animation
//    and emit a Serum entry: (CRC32, 64-colour RGB palette, 4096-byte indexed
//    pixel frame).
// 4. Serialise the SerumData struct via its own SaveToFile() which produces a
//    libserum-compatible .cROMc file.
//
// Pixel mapping note
// ──────────────────
// PIN2DMD ColorMask VNI frames use 7 bit-planes (values 0–127).  The upper bit
// (bit 6, values 64–127) flags a pixel as "apply colour from palette[val & 63]".
// Values 0–63 are passthrough (original DMD shade).  For the Serum output we
// mask all pixels to 6 bits so every value is a valid palette index.

#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

// libserum internals (static library)
#include "SerumData.h"

// heatshrink C API
extern "C" {
#include "heatshrink_decoder.h"
}

// ── Endian helpers ────────────────────────────────────────────────────────────

static uint16_t be16(const uint8_t *p) {
  return (uint16_t)(p[0] << 8 | p[1]);
}
static uint32_t be32(const uint8_t *p) {
  return (uint32_t)(p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3]);
}

// ── File I/O ──────────────────────────────────────────────────────────────────

static std::vector<uint8_t> read_file(const char *path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error(std::string("cannot open: ") + path);
  return {std::istreambuf_iterator<char>(f), {}};
}

// ── Heatshrink ────────────────────────────────────────────────────────────────

static std::vector<uint8_t> hs_decompress(const uint8_t *src, size_t src_len) {
  heatshrink_decoder *hsd = heatshrink_decoder_alloc(256, 10, 5);
  if (!hsd) throw std::runtime_error("heatshrink_decoder_alloc failed");

  // heatshrink API takes non-const; data is only read so the cast is safe
  size_t sunk = 0;
  if (heatshrink_decoder_sink(hsd, const_cast<uint8_t *>(src), src_len,
                               &sunk) != HSDR_SINK_OK ||
      sunk != src_len) {
    heatshrink_decoder_free(hsd);
    throw std::runtime_error("heatshrink sink failed");
  }
  heatshrink_decoder_finish(hsd);

  std::vector<uint8_t> out;
  uint8_t buf[4096];
  size_t polled;
  HSD_poll_res pr;
  do {
    pr = heatshrink_decoder_poll(hsd, buf, sizeof(buf), &polled);
    out.insert(out.end(), buf, buf + polled);
  } while (pr == HSDR_POLL_MORE);

  heatshrink_decoder_free(hsd);
  return out;
}

// ── Bit-plane decoder ─────────────────────────────────────────────────────────

// VNI stores planes MSB-first within each byte: bit 7 of byte b is the leftmost
// pixel of its 8-pixel group.  Reading bit (7 - i%8) gives the correct spatial
// order — verified by rendering: with LSB the colorized frames come out
// horizontally mirrored vs the live DMD ("TWO TOWERS JACKPOT" etc.).  (This is
// the *display* order for cframes; the trigger hash uses the live frame and a
// separate LSB plane packing, so the two conventions are independent.)
static std::vector<uint8_t> decode_planes(const uint8_t *planes_raw,
                                          int bit_depth, int plane_size,
                                          int width, int height) {
  const int npixels = width * height;
  std::vector<uint8_t> pixels(npixels, 0);
  for (int p = 0; p < bit_depth; ++p) {
    const uint8_t *plane = planes_raw + p * plane_size;
    for (int i = 0; i < npixels; ++i) {
      int bit = (plane[i / 8] >> (7 - (i % 8))) & 1;
      pixels[i] |= (uint8_t)(bit << p);
    }
  }
  return pixels;
}

// ── CRC32 (standard, matches libserum crc32_fast and zlib) ─────────────────────

static uint32_t g_crc_table[256];
static void crc32_init() {
  for (uint32_t i = 0; i < 256; ++i) {
    uint32_t ch = i, crc = 0;
    for (int j = 0; j < 8; ++j) {
      uint32_t b = (ch ^ crc) & 1;
      crc >>= 1;
      if (b) crc ^= 0xEDB88320u;
      ch >>= 1;
    }
    g_crc_table[i] = crc;
  }
}
static uint32_t crc32_buf(const uint8_t *s, size_t n) {
  uint32_t crc = 0xffffffffu;
  for (size_t i = 0; i < n; ++i)
    crc = (crc >> 8) ^ g_crc_table[(s[i] ^ crc) & 0xFF];
  return ~crc;
}

// Pack bit `plane` of each pixel into an LSB-ordered bit-plane (512 bytes for a
// 128×32 frame).
static std::vector<uint8_t> pack_plane(const uint8_t *px, int npixels,
                                       int plane) {
  std::vector<uint8_t> packed(npixels / 8, 0);
  for (int j = 0; j < npixels; ++j)
    if ((px[j] >> plane) & 1) packed[j / 8] |= (uint8_t)(1 << (j % 8));
  return packed;
}

// CRC32 of one bit-plane.  Unmasked: how PIN2DMD hashes Replace-mode triggers.
static uint32_t plane_crc(const uint8_t *px, int npixels, int plane) {
  auto packed = pack_plane(px, npixels, plane);
  return crc32_buf(packed.data(), packed.size());
}

// CRC32 of a bit-plane AND-ed with a 512-byte mask (keep where mask bit set).
// This is how PIN2DMD hashes ColorMask / LayeredColorMask triggers: only the
// mask's static region contributes.  (Discovered via scripts/discover_masks.py.)
static uint32_t masked_plane_crc(const std::vector<uint8_t> &plane,
                                 const std::vector<uint8_t> &mask512) {
  std::vector<uint8_t> m(plane.size());
  for (size_t i = 0; i < plane.size(); ++i) m[i] = plane[i] & mask512[i];
  return crc32_buf(m.data(), m.size());
}

// Build a libserum compmask (one byte per pixel) from a PIN2DMD 512-byte mask.
// libserum's crc32_fast_mask hashes pixels where compmask==0, so to hash
// PIN2DMD's *set* (static) region we invert: 0 where the mask bit is set.
static std::vector<uint8_t> serum_compmask(const std::vector<uint8_t> &mask512,
                                           int npixels) {
  std::vector<uint8_t> cm(npixels);
  for (int i = 0; i < npixels; ++i)
    cm[i] = ((mask512[i / 8] >> (i % 8)) & 1) ? 0 : 1;
  return cm;
}

// The hashcode libserum will recompute live for a masked frame:
// crc32 over the per-pixel values where the compmask is 0 (= mask bit set).
static uint32_t masked_serum_crc(const uint8_t *px, int npixels,
                                 const std::vector<uint8_t> &mask512) {
  std::vector<uint8_t> sel;
  sel.reserve(npixels);
  for (int i = 0; i < npixels; ++i)
    if ((mask512[i / 8] >> (i % 8)) & 1) sel.push_back(px[i]);
  return crc32_buf(sel.data(), sel.size());
}

// ── PAL parser ────────────────────────────────────────────────────────────────

static const int NCOLORS = 64;    // colours per palette
static const int PAL_BYTES = NCOLORS * 3;

struct Palette {
  uint16_t idx;
  uint8_t  type;
  uint8_t  rgb[NCOLORS * 3];  // R0 G0 B0 R1 G1 B1 ...
};

struct Mapping {
  uint32_t crc32;
  uint8_t  mode;
  uint16_t pal_idx;
  uint32_t vni_off;
};

struct PalFile {
  std::map<uint16_t, Palette>            palettes;  // keyed by palette index
  std::vector<Mapping>                   mappings;
  std::vector<std::vector<uint8_t>>      masks;     // 512-byte (128×32 bit) masks
};

static PalFile parse_pal(const std::vector<uint8_t> &d) {
  size_t off = 0;
  /* version = */ off++;
  uint16_t npal = be16(&d[off]); off += 2;

  PalFile pf;
  for (int i = 0; i < npal; ++i) {
    Palette p{};
    p.idx = be16(&d[off]); off += 2;
    uint16_t nc = be16(&d[off]); off += 2;
    p.type = d[off++];
    for (int c = 0; c < nc && c < NCOLORS; ++c) {
      p.rgb[c * 3]     = d[off];
      p.rgb[c * 3 + 1] = d[off + 1];
      p.rgb[c * 3 + 2] = d[off + 2];
      off += 3;
    }
    // skip extra colours beyond NCOLORS
    if (nc > NCOLORS) off += (nc - NCOLORS) * 3;
    pf.palettes[p.idx] = p;
  }

  uint16_t nmaps = be16(&d[off]); off += 2;
  for (int i = 0; i < nmaps; ++i) {
    Mapping m{};
    m.crc32   = be32(&d[off]); off += 4;
    m.mode    = d[off++];
    m.pal_idx = be16(&d[off]); off += 2;
    m.vni_off = be32(&d[off]); off += 4;
    pf.mappings.push_back(m);
  }

  // Masks: uint8 count, then 512 bytes (128×32 bit mask) each.  Used by the
  // ColorMask / LayeredColorMask trigger hashing (see bridge mode).
  if (off < d.size()) {
    uint8_t nmasks = d[off++];
    for (int i = 0; i < nmasks && off + 512 <= d.size(); ++i) {
      pf.masks.emplace_back(&d[off], &d[off] + 512);
      off += 512;
    }
  }
  return pf;
}

// ── VNI parser ────────────────────────────────────────────────────────────────

struct VniFrame {
  uint32_t             hash;
  uint16_t             delay_ms;
  std::vector<uint8_t> pixels;      // indexed-colour bytes, masked to 0..63
  std::vector<uint8_t> pixels_raw;  // full 0..127 (bit6 = colorised flag)
};

struct Animation {
  std::string           name;
  std::vector<VniFrame> frames;
};

// Parse ONE animation starting at byte offset `start` in `d`.
// Returns number of bytes consumed.
static Animation parse_animation(const std::vector<uint8_t> &d, size_t start,
                                 int version) {
  size_t off = start;
  Animation anim;

  // Name
  uint16_t name_len = be16(&d[off]); off += 2;
  anim.name.assign((const char *)&d[off], name_len); off += name_len;

  // 16 bytes of deprecated fields
  off += 16;

  uint16_t nframes = be16(&d[off]); off += 2;

  // Version 2+: reserved + embedded palette (we use the PAL file palette)
  int num_colors_embedded = 0;
  if (version >= 2) {
    off += 2;  // reserved
    num_colors_embedded = be16(&d[off]); off += 2;
    off += num_colors_embedded * 3;
  }

  if (version >= 3) off++;  // edit mode

  int width = 128, height = 32;
  if (version >= 4) {
    width  = be16(&d[off]); off += 2;
    height = be16(&d[off]); off += 2;
  }

  // Version 5: per-animation masks (skip)
  if (version >= 5) {
    uint16_t nmasks = be16(&d[off]); off += 2;
    for (int i = 0; i < nmasks; ++i) {
      off++;  // locked flag
      uint16_t mask_size = be16(&d[off]); off += 2;
      off += mask_size;
    }
  }

  // Version 6: compiled animation data (skip)
  if (version >= 6) {
    off++;  // compiled flag
    uint16_t compiled_size = be16(&d[off]); off += 2;
    off += compiled_size;
    off += 4;  // start_frame
  }

  const int plane_size = (width * height) / 8;

  for (int fi = 0; fi < nframes; ++fi) {
    /* raw_plane_size (legacy, may be negative) */ off += 2;
    uint16_t delay_ms = be16(&d[off]); off += 2;

    uint32_t hash = 0;
    if (version >= 4) { hash = be32(&d[off]); off += 4; }

    int bit_depth = d[off++];
    bool compressed = false;
    if (version >= 3) compressed = (d[off++] != 0);

    std::vector<uint8_t> planes_raw;
    if (compressed) {
      uint32_t comp_size = be32(&d[off]); off += 4;
      planes_raw = hs_decompress(&d[off], comp_size);
      off += comp_size;
    } else {
      planes_raw.resize(bit_depth * plane_size);
      for (int p = 0; p < bit_depth; ++p) {
        /* plane marker */ off++;
        memcpy(planes_raw.data() + p * plane_size, &d[off], plane_size);
        off += plane_size;
      }
    }

    if ((int)planes_raw.size() < bit_depth * plane_size) {
      fprintf(stderr, "  warn: short planes in '%s' frame %d, padding\n",
              anim.name.c_str(), fi);
      planes_raw.resize(bit_depth * plane_size, 0);
    }

    VniFrame frame;
    frame.hash     = hash;
    frame.delay_ms = delay_ms;
    frame.pixels   = decode_planes(planes_raw.data(), bit_depth, plane_size,
                                   width, height);
    frame.pixels_raw = frame.pixels;  // keep full 0..127 (bit6 = colorised)
    // Mask to 6 bits so every value is a valid palette index (0–63)
    for (auto &px : frame.pixels) px &= 0x3F;

    anim.frames.push_back(std::move(frame));
  }

  return anim;
}

// Parse the VNI file.  Returns a map: file_byte_offset → Animation.
static std::map<uint32_t, Animation> parse_vni(const std::vector<uint8_t> &d) {
  if (d.size() < 8 || memcmp(d.data(), "VPIN", 4) != 0)
    throw std::runtime_error("bad VNI magic");

  int version  = be16(&d[4]);
  int nanims   = be16(&d[6]);
  fprintf(stderr, "VNI version=%d, animations=%d\n", version, nanims);

  // Offset table (version >= 2 always true for our data, version=5)
  std::vector<uint32_t> offsets(nanims);
  for (int i = 0; i < nanims; ++i)
    offsets[i] = be32(&d[8 + i * 4]);

  std::map<uint32_t, Animation> result;
  for (int i = 0; i < nanims; ++i) {
    Animation anim = parse_animation(d, offsets[i], version);
    result[offsets[i]] = std::move(anim);
  }
  return result;
}

// ── Main conversion ───────────────────────────────────────────────────────────

static void usage(const char *prog) {
  fprintf(stderr,
          "Usage: %s --pal <file.pal> --vni <file.vni> "
          "--rom <romname> --out <file.cROMc> [--frames <dir>]\n"
          "  --frames <dir>  bridge mode: key triggers off captured .bin frames\n"
          "                  in <dir> instead of raw PAL CRCs (see source)\n",
          prog);
}

int main(int argc, char **argv) {
  const char *pal_path = nullptr, *vni_path = nullptr;
  const char *rom_name = nullptr, *out_path = nullptr;
  std::vector<std::string> frame_dirs;  // bridge mode: captured .bin frame dirs
                                        // (repeatable; each scanned recursively)
  bool selftest_vni = false;            // diagnostic: can VNI frames reproduce
                                        // the PAL trigger CRCs directly?
  const char *dump_vni = nullptr;       // dir: write each mapping's colorized
                                        // first frame as a PPM (covered/uncovered)
  bool learn_shade = false;             // diagnostic: is colorized value → orig
                                        // plane bit deterministic (covered pairs)?

  for (int i = 1; i < argc; ++i) {
    if      (!strcmp(argv[i], "--pal") && i+1 < argc) pal_path = argv[++i];
    else if (!strcmp(argv[i], "--vni") && i+1 < argc) vni_path = argv[++i];
    else if (!strcmp(argv[i], "--rom") && i+1 < argc) rom_name = argv[++i];
    else if (!strcmp(argv[i], "--out") && i+1 < argc) out_path = argv[++i];
    else if (!strcmp(argv[i], "--frames") && i+1 < argc)
      frame_dirs.push_back(argv[++i]);
    else if (!strcmp(argv[i], "--selftest-vni")) selftest_vni = true;
    else if (!strcmp(argv[i], "--dump-vni") && i+1 < argc) dump_vni = argv[++i];
    else if (!strcmp(argv[i], "--learn-shade")) learn_shade = true;
    else { usage(argv[0]); return 1; }
  }
  bool need_out = !selftest_vni && !dump_vni;
  if (!pal_path || !vni_path || (need_out && (!rom_name || !out_path))) {
    usage(argv[0]); return 1;
  }
  crc32_init();

  fprintf(stderr, "Loading %s ...\n", pal_path);
  auto pal_data = read_file(pal_path);
  PalFile pf = parse_pal(pal_data);
  fprintf(stderr, "  %zu palettes, %zu mappings\n",
          pf.palettes.size(), pf.mappings.size());

  fprintf(stderr, "Loading %s ...\n", vni_path);
  auto vni_data = read_file(vni_path);
  auto animations = parse_vni(vni_data);
  fprintf(stderr, "  %zu animations\n", animations.size());

  if (selftest_vni) {
    // Can the colorized VNI first-frame reproduce the PAL trigger CRC (the
    // plane-CRC of the *original* DMD frame)?  Test several reductions of the
    // VNI pixels against each mapping's own CRC, by mode.
    std::map<std::string, std::map<int,int>> hits;  // scheme → mode → count
    std::map<int,int> totals;
    for (const auto &m : pf.mappings) {
      totals[m.mode]++;
      auto it = animations.find(m.vni_off);
      if (it == animations.end() || it->second.frames.empty()) continue;
      const auto &px = it->second.frames[0].pixels;  // already &0x3F (0..63)
      if (px.size() != 128 * 32) continue;
      // candidate reductions of the colorized pixel to an "original" value
      std::vector<uint8_t> raw(px.begin(), px.end());               // as-is 0..63
      std::vector<uint8_t> bin(px.size());                          // lit/unlit
      for (size_t i = 0; i < px.size(); ++i) bin[i] = px[i] ? 1 : 0;
      auto test = [&](const char *name, const std::vector<uint8_t> &f) {
        for (int p = 0; p < 2; ++p)
          if (plane_crc(f.data(), 128 * 32, p) == m.crc32) {
            hits[name][m.mode]++; return true;
          }
        // masked variants for mask modes
        auto pl0 = pack_plane(f.data(), 128 * 32, 0);
        for (auto &mk : pf.masks)
          if (masked_plane_crc(pl0, mk) == m.crc32) {
            hits[std::string(name) + "+mask"][m.mode]++; return true;
          }
        return false;
      };
      test("raw", raw);
      test("bin", bin);
    }
    fprintf(stderr, "\n=== VNI self-test: can VNI reproduce PAL trigger CRCs? ===\n");
    fprintf(stderr, "PAL mappings by mode:");
    for (auto [mode, n] : totals) fprintf(stderr, " mode%d=%d", mode, n);
    fprintf(stderr, "\n");
    if (hits.empty()) fprintf(stderr, "  NO scheme reproduced any CRC\n");
    for (auto &[scheme, bymode] : hits) {
      fprintf(stderr, "  scheme '%s':", scheme.c_str());
      for (auto [mode, n] : bymode) fprintf(stderr, " mode%d=%d", mode, n);
      fprintf(stderr, "\n");
    }
    return 0;
  }

  // ── Build Serum frames ──────────────────────────────────────────────────────

  std::vector<uint32_t>             frame_hashes;
  std::vector<std::vector<uint8_t>> frame_palettes;  // 192 bytes each
  std::vector<std::vector<uint8_t>> frame_pixels;    // 4096 bytes each
  std::vector<int>                  frame_maskid;    // -1 = no mask (full frame)
  std::set<uint32_t>                covered_crcs;    // PAL CRCs a frame matched
  // learn-shade diagnostic (per covered Replace frame): is colorized value →
  // original plane0 bit a function *within that frame*?  If so, brute-force
  // free vars = distinct values (small); if values are internally mixed, those
  // pixels become individually free (blows up).
  long ls_frames = 0, ls_consistent = 0, ls_sum_distinct = 0,
       ls_sum_mixed_px = 0, ls_max_distinct = 0;

  // Compmask pool: PIN2DMD mask index → libserum compmask id (dedup; only masks
  // actually used are stored).  compmask_data[id] is a 4096-byte per-pixel mask.
  std::map<int, int>                pin_to_compid;
  std::vector<std::vector<uint8_t>> compmask_data;

  // Helper shared by both modes: append one Serum entry for a PAL mapping,
  // keyed by the supplied libserum-domain hashcode.  `maskid` is the libserum
  // compmask id (-1 = unmasked, full-frame hash).  Returns false if the mapping
  // can't be realised (missing animation/palette) or the hashcode is a dup.
  std::map<uint32_t, int> seen_hash;  // serum hashcode → first frame index
  auto emit_entry = [&](const Mapping &m, uint32_t hashcode,
                        int maskid) -> bool {
    auto anim_it = animations.find(m.vni_off);
    if (anim_it == animations.end() || anim_it->second.frames.empty())
      return false;
    auto pal_it = pf.palettes.find(m.pal_idx);
    if (pal_it == pf.palettes.end()) return false;
    const VniFrame &frame = anim_it->second.frames[0];
    if (frame.pixels.size() != 128 * 32) return false;
    if (seen_hash.count(hashcode)) return false;
    seen_hash[hashcode] = (int)frame_hashes.size();
    frame_hashes.push_back(hashcode);
    frame_palettes.push_back(std::vector<uint8_t>(
        pal_it->second.rgb, pal_it->second.rgb + PAL_BYTES));
    frame_pixels.push_back(frame.pixels);
    frame_maskid.push_back(maskid);
    return true;
  };

  if (!frame_dirs.empty()) {
    // ── Bridge mode ──────────────────────────────────────────────────────────
    // The PAL trigger CRC32 is computed by PIN2DMD over a packed bit-plane,
    // which libserum (hashing the per-pixel frame) can never reproduce.  Bridge
    // the two domains using real captured frames: for each captured per-pixel
    // frame F, compute its plane CRC32 (→ look up PIN2DMD colorization) AND its
    // full-frame CRC32 (→ the hashcode libserum will recompute live).
    //
    // Each --frames dir is scanned recursively, so several gameplay sessions
    // can be accumulated into one cROMc (a trigger seen in any session is
    // emitted once; emit_entry dedups by serum hashcode).
    std::map<uint32_t, const Mapping *> crc_to_map;
    for (const auto &m : pf.mappings) crc_to_map.emplace(m.crc32, &m);

    int scanned = 0, matched = 0, emitted = 0;
    std::map<int, int> emitted_by_mode;  // PAL mode → unique triggers emitted
    for (const auto &dir : frame_dirs) {
      int dir_emitted = emitted;
      fprintf(stderr, "Bridge: scanning %s ...\n", dir.c_str());
      for (const auto &de :
           std::filesystem::recursive_directory_iterator(dir)) {
        if (de.path().extension() != ".bin") continue;
        auto buf = read_file(de.path().c_str());
        if (buf.size() != 128 * 32) continue;
        ++scanned;
        const uint8_t *px = buf.data();
        bool hit = false;

        // 1) Unmasked plane CRC → Replace (1) / Follow (4) triggers.
        //    libserum will recompute the full-frame CRC live, so store that.
        for (int plane = 0; plane < 2 && !hit; ++plane) {
          auto it = crc_to_map.find(plane_crc(px, 128 * 32, plane));
          if (it == crc_to_map.end()) continue;
          if (it->second->mode != 1 && it->second->mode != 4) continue;
          ++matched;
          covered_crcs.insert(it->second->crc32);
          if (learn_shade) {
            // Per-frame: pair this captured original (px, 0..3) with the
            // mapping's colorized VNI frame; check whether colorized value →
            // plane0 bit is a function within this frame.
            auto av = animations.find(it->second->vni_off);
            if (av != animations.end() && !av->second.frames.empty()) {
              const auto &vr = av->second.frames[0].pixels_raw;
              if (vr.size() == 128 * 32) {
                uint8_t st[128] = {0};  // 0 unseen,1 only-0,2 only-1,3 mixed
                long cnt[128] = {0};
                for (int i = 0; i < 128 * 32; ++i) {
                  int b = px[i] & 1;
                  st[vr[i]] |= (b ? 2 : 1);
                  cnt[vr[i]]++;
                }
                int distinct = 0, mixed_px = 0;
                bool consistent = true;
                for (int v = 0; v < 128; ++v) {
                  if (!st[v]) continue;
                  distinct++;
                  if (st[v] == 3) { consistent = false; mixed_px += cnt[v]; }
                }
                ls_frames++;
                ls_sum_distinct += distinct;
                ls_sum_mixed_px += mixed_px;
                if (distinct > ls_max_distinct) ls_max_distinct = distinct;
                if (consistent) ls_consistent++;
              }
            }
          }
          if (emit_entry(*it->second, crc32_buf(px, 128 * 32), -1)) {
            ++emitted; ++emitted_by_mode[it->second->mode];
          }
          hit = true;
        }

        // 2) Masked plane CRC → ColorMask (2) / LayeredColorMask (5) triggers.
        //    PIN2DMD hashes plane0 AND mask (set region); we look up by that,
        //    then store the libserum masked hash (per-pixel, static region) so
        //    one trigger fires across all dynamic-region variations.
        for (size_t mi = 0; mi < pf.masks.size() && !hit; ++mi) {
          auto plane0 = pack_plane(px, 128 * 32, 0);
          auto it = crc_to_map.find(masked_plane_crc(plane0, pf.masks[mi]));
          if (it == crc_to_map.end()) continue;
          if (it->second->mode != 2 && it->second->mode != 5) continue;
          ++matched;
          covered_crcs.insert(it->second->crc32);
          // Assign / reuse a libserum compmask id for this PIN2DMD mask.
          auto [cit, fresh] = pin_to_compid.emplace((int)mi,
                                                    (int)compmask_data.size());
          if (fresh) compmask_data.push_back(serum_compmask(pf.masks[mi], 128 * 32));
          uint32_t hashcode = masked_serum_crc(px, 128 * 32, pf.masks[mi]);
          if (emit_entry(*it->second, hashcode, cit->second)) {
            ++emitted; ++emitted_by_mode[it->second->mode];
          }
          hit = true;
        }
      }
      fprintf(stderr, "  (+%d new triggers from this dir)\n",
              emitted - dir_emitted);
    }
    fprintf(stderr, "  scanned=%d matched=%d emitted=%d (unique triggers)\n",
            scanned, matched, emitted);
    fprintf(stderr, "  by PAL mode:");
    for (auto [mode, n] : emitted_by_mode)
      fprintf(stderr, " mode%d=%d", mode, n);
    fprintf(stderr, "  | compmasks used=%zu\n", compmask_data.size());

    if (learn_shade && ls_frames) {
      fprintf(stderr,
              "  learn-shade (per covered Replace frame, n=%ld):\n"
              "    frames where colorized value→plane0 bit is fully consistent: "
              "%ld/%ld (%.1f%%)\n"
              "    avg distinct colorized values/frame: %.1f (max %ld) "
              "→ brute force 2^that if consistent\n"
              "    avg mixed (internally-inconsistent) pixels/frame: %.1f "
              "→ these become individually-free bits when inconsistent\n",
              ls_frames, ls_consistent, ls_frames,
              100.0 * (double)ls_consistent / (double)ls_frames,
              (double)ls_sum_distinct / (double)ls_frames, ls_max_distinct,
              (double)ls_sum_mixed_px / (double)ls_frames);
    }
  } else {
    // ── Default mode (PAL CRC as hashcode — for inspection only) ──────────────
    int skipped = 0;
    for (const auto &m : pf.mappings)
      if (!emit_entry(m, m.crc32, -1)) ++skipped;
    fprintf(stderr, "  %zu Serum frames (%d skipped)\n", frame_hashes.size(),
            skipped);
  }

  if (dump_vni) {
    // Render every mapping's colorized VNI first-frame to a PPM, split into
    // covered/ and uncovered/ so we can SEE which triggers we still lack frames
    // for (and what game screen each is).  Colorize = palette[pixel index].
    namespace fs = std::filesystem;
    fs::create_directories(std::string(dump_vni) + "/covered");
    fs::create_directories(std::string(dump_vni) + "/uncovered");
    int wrote = 0;
    for (const auto &m : pf.mappings) {
      auto anim_it = animations.find(m.vni_off);
      if (anim_it == animations.end() || anim_it->second.frames.empty()) continue;
      auto pal_it = pf.palettes.find(m.pal_idx);
      if (pal_it == pf.palettes.end()) continue;
      const auto &px = anim_it->second.frames[0].pixels;
      if (px.size() != 128 * 32) continue;
      const uint8_t *rgb = pal_it->second.rgb;
      std::vector<uint8_t> img(128 * 32 * 3);
      for (size_t i = 0; i < px.size(); ++i) {
        uint8_t idx = px[i] & 0x3F;
        img[i * 3] = rgb[idx * 3];
        img[i * 3 + 1] = rgb[idx * 3 + 1];
        img[i * 3 + 2] = rgb[idx * 3 + 2];
      }
      char name[256];
      snprintf(name, sizeof(name), "%s/%s/m%u_%08x_pal%u.ppm", dump_vni,
               covered_crcs.count(m.crc32) ? "covered" : "uncovered",
               m.mode, m.crc32, m.pal_idx);
      std::ofstream f(name, std::ios::binary);
      f << "P6\n128 32\n255\n";
      f.write((const char *)img.data(), img.size());
      ++wrote;
    }
    fprintf(stderr, "Dumped %d colorized frames to %s (covered/ + uncovered/)\n",
            wrote, dump_vni);
    return 0;
  }

  if (frame_hashes.empty()) {
    fprintf(stderr, "Error: no frames to write\n");
    return 1;
  }

  // ── Populate SerumData ─────────────────────────────────────────────────────

  SerumData sd;
  memset(sd.rname, 0, sizeof(sd.rname));
  strncpy(sd.rname, rom_name, sizeof(sd.rname) - 1);
  sd.SerumVersion  = SERUM_V1;
  sd.fwidth        = 128;
  sd.fheight       = 32;
  sd.fwidth_extra  = 0;
  sd.fheight_extra = 0;
  sd.nframes       = (uint32_t)frame_hashes.size();
  sd.nocolors      = 4;       // 4-shade DMD input (2 bits/pixel)
  sd.nccolors      = NCOLORS; // 64-entry colorization palette
  sd.ncompmasks    = (uint32_t)compmask_data.size();
  sd.nmovmasks     = 0;
  sd.nsprites      = 0;
  sd.nbackgrounds  = 0;
  sd.is256x64      = false;

  // Compmasks (one 4096-byte per-pixel mask per used PIN2DMD mask).
  for (uint32_t id = 0; id < sd.ncompmasks; ++id)
    sd.compmasks.set(id, compmask_data[id].data(), 128 * 32);

  for (uint32_t i = 0; i < sd.nframes; ++i) {
    // hashcodes is useIndex=true — use the new setAtIndex method
    sd.hashcodes.setAtIndex(i, &frame_hashes[i], 1);

    // cpal and cframes are useIndex=false — use set()
    sd.cpal.set(i, frame_palettes[i].data(), PAL_BYTES);
    sd.cframes.set(i, frame_pixels[i].data(), 128 * 32);

    // Masked triggers: point the frame at its compmask (shapecompmode stays 0).
    // Unmasked frames leave compmaskID at its 255 default (no mask).
    if (frame_maskid[i] >= 0) {
      uint8_t id = (uint8_t)frame_maskid[i];
      sd.compmaskID.set(i, &id, 1);
    }
  }

  // ── Write output ───────────────────────────────────────────────────────────
  fprintf(stderr, "Writing %s ...\n", out_path);
  if (!sd.SaveToFile(out_path)) {
    fprintf(stderr, "SaveToFile failed\n");
    return 1;
  }

  fprintf(stderr, "Done.  %u frames written to %s\n", sd.nframes, out_path);
  return 0;
}
