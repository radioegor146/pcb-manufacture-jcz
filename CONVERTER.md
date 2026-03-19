# pcb-manufacture-jcz

Generate SVGs from PCB Gerber files for laser manufacturing (LightBurn).

## Setup

```sh
uv venv
uv pip install -r requirements.txt
```

## Usage

### Copper layer

Generates inverted SVG (black = etch, white = keep copper):

```sh
uv run python3 convert.py copper [--flip] [--brim <mm>] [--linearization-step <deg>] [-o <output.svg>] <edge_cuts.gbr> <copper.gbr>
```

### Edge cuts & drill holes

Generates SVG with board outline and drill holes:

```sh
uv run python3 convert.py cuts [--flip] [--brim <mm>] [-o <output.svg>] <edge_cuts.gbr> [drl files...]
```

### Silkscreen

Generates SVG with silkscreen layer, sized to match PCB outline:

```sh
uv run python3 convert.py silk [--flip] [--brim <mm>] [--linearization-step <deg>] [-o <output.svg>] <edge_cuts.gbr> <silkscreen.gbr>
```

### KiCad batch mode

Auto-detects all Gerber/drill files in a KiCad export directory and generates all applicable SVGs:

```sh
uv run python3 convert.py kicad [--brim <mm>] [--linearization-step <deg>] [-o <output-dir>] <gerber-directory>
```

Looks for `*-Edge_Cuts.gbr`, `*-F_Cu.gbr`, `*-B_Cu.gbr`, `*-F_Silkscreen.gbr`, `*-B_Silkscreen.gbr`, `*-PTH.drl`, `*-NPTH.drl` and outputs to `<gerber-directory>/jcz-manufacture/`:

- `copper_top.svg` / `copper_bottom.svg`
- `silk_top.svg` / `silk_bottom.svg`
- `cuts.svg`

### Options

- `--flip` — mirror on X axis (for bottom layers, individual commands only)
- `--brim <mm>` — margin around PCB in mm (default: 1)
- `--linearization-step <deg>` — arc linearization step in degrees (default: 1). Smaller values produce smoother arcs, larger values reduce SVG file size. Available on `copper`, `silk`, and `kicad` commands.
- `-o` — output SVG path (individual) or directory (kicad)
