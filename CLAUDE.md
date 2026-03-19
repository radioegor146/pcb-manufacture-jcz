# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python script that generates SVG files from PCB Gerber files (edge cuts, copper layers) and drill files. Designed for PCB manufacturing workflows.

## Commands

```sh
# Activate virtual environment (uses uv)
source .venv/bin/activate

# Generate copper cuts SVG
python3 convert.py copper [--flip] [--brim <mm>] [--linearization-step <deg>] [-o <output.svg>] <edge_cuts.gbr> <copper.gbr>

# Generate holes/edge cuts SVG
python3 convert.py cuts [--flip] [--brim <mm>] [-o <output.svg>] <edge_cuts.gbr> ...[drl files]

# Generate silkscreen SVG
python3 convert.py silk [--flip] [--brim <mm>] [--linearization-step <deg>] [-o <output.svg>] <edge_cuts.gbr> <silkscreen.gbr>

# Batch convert KiCad export directory
python3 convert.py kicad [--brim <mm>] [--linearization-step <deg>] [-o <output-dir>] <gerber-directory>
```

Use `--flip` for bottom layer gerbers (flips on X axis).
Use `--brim` to set margin around PCB in mm (default: 1).
Use `--linearization-step` to set arc linearization step in degrees (default: 1). Smaller = smoother arcs, larger = smaller SVG files.

## Dependencies

- Python 3.14 (managed via uv)
- pygerber - Gerber/Excellon file parsing
- click - CLI framework
- numpy, pillow, pydantic - Supporting libraries
