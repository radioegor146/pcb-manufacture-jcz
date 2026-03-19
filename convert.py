#!/usr/bin/env python3
"""Convert PCB Gerber files to SVG for manufacturing (LightBurn)."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from typing import Optional

import click
import drawsvg
from shapely import make_valid
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from pygerber.backend.rasterized_2d.color_scheme import ColorScheme
from pygerber.common.rgba import RGBA
from pygerber.gerberx3.api._v2 import GerberFile, OnParserErrorEnum
from pygerber.gerberx3.math.bounding_box import BoundingBox
from pygerber.gerberx3.math.offset import Offset
from pygerber.gerberx3.parser2.apertures2.circle2 import Circle2
from pygerber.gerberx3.parser2.apertures2.macro2 import Macro2
from pygerber.gerberx3.parser2.apertures2.obround2 import Obround2
from pygerber.gerberx3.parser2.apertures2.polygon2 import Polygon2
from pygerber.gerberx3.parser2.apertures2.rectangle2 import Rectangle2
from pygerber.gerberx3.parser2.command_buffer2 import ReadonlyCommandBuffer2
from pygerber.gerberx3.parser2.commands2.arc2 import Arc2, CCArc2
from pygerber.gerberx3.parser2.commands2.flash2 import Flash2
from pygerber.gerberx3.parser2.commands2.line2 import Line2
from pygerber.gerberx3.parser2.commands2.region2 import Region2
from pygerber.gerberx3.renderer2.svg import SvgRenderer2, SvgRenderer2Hooks
from pygerber.gerberx3.state_enums import Mirroring


# Black on white color scheme for LightBurn
MANUFACTURING_SCHEME = ColorScheme(
    background_color=RGBA.from_hex("#FFFFFFFF"),
    clear_color=RGBA.from_hex("#FFFFFFFF"),
    solid_color=RGBA.from_hex("#000000FF"),
    clear_region_color=RGBA.from_hex("#FFFFFFFF"),
    solid_region_color=RGBA.from_hex("#000000FF"),
)

ARC_RESOLUTION_DEG = 1.0


def get_stroke_width_safe(aperture) -> float:
    """Get stroke width in mm, falling back to 0.1mm for unsupported apertures."""
    try:
        return float(aperture.get_stroke_width().as_millimeters())
    except NotImplementedError:
        return 0.1


@dataclass
class DrillHole:
    """Represents a drill hole from Excellon file."""
    x: Decimal
    y: Decimal
    diameter: Decimal


def parse_excellon(path: str) -> list[DrillHole]:
    """Parse Excellon drill file and extract hole positions."""
    holes: list[DrillHole] = []
    tools: dict[int, Decimal] = {}
    current_tool: Optional[int] = None

    with open(path, 'r') as f:
        for line in f:
            line = line.strip()

            # Tool definition: T01C0.950 or T1C0.3
            if match := re.match(r'T(\d+)C([0-9.]+)', line):
                tool_num = int(match.group(1))
                diameter = Decimal(match.group(2))
                tools[tool_num] = diameter
                continue

            # Tool selection: T01 or T1
            if match := re.match(r'^T(\d+)$', line):
                current_tool = int(match.group(1))
                continue

            # Drill position: X11.75Y48.0
            if match := re.match(r'X([+-]?\d+\.?\d*)Y([+-]?\d+\.?\d*)', line):
                if current_tool is not None:
                    x = Decimal(match.group(1))
                    y = Decimal(match.group(2))
                    diameter = tools.get(current_tool, Decimal("0"))
                    holes.append(DrillHole(x=x, y=y, diameter=diameter))

    return holes


def parse_gerber(path: str) -> ReadonlyCommandBuffer2:
    """Parse Gerber file and return command buffer."""
    gerber = GerberFile.from_file(path)
    parsed = gerber.parse(on_parser_error=OnParserErrorEnum.Warn)
    return parsed._command_buffer


def get_bounding_box(cmd_buffer: ReadonlyCommandBuffer2) -> BoundingBox:
    """Get bounding box from command buffer."""
    return cmd_buffer.get_bounding_box()


def apply_flip(cmd_buffer: ReadonlyCommandBuffer2, flip: bool) -> ReadonlyCommandBuffer2:
    """Apply X-axis flip to command buffer if requested."""
    if flip:
        return cmd_buffer.get_mirrored(Mirroring.X)
    return cmd_buffer


def path_rect(x: float, y: float, w: float, h: float, rx: float = 0, **kwargs) -> drawsvg.Path:
    """Create a rectangle as a <path> element (LightBurn compatible)."""
    p = drawsvg.Path(**kwargs)
    if rx > 0:
        p.M(x + rx, y)
        p.L(x + w - rx, y)
        p.A(rx, rx, 0, 0, 1, x + w, y + rx)
        p.L(x + w, y + h - rx)
        p.A(rx, rx, 0, 0, 1, x + w - rx, y + h)
        p.L(x + rx, y + h)
        p.A(rx, rx, 0, 0, 1, x, y + h - rx)
        p.L(x, y + rx)
        p.A(rx, rx, 0, 0, 1, x + rx, y)
    else:
        p.M(x, y)
        p.L(x + w, y)
        p.L(x + w, y + h)
        p.L(x, y + h)
    p.Z()
    return p


def path_circle(cx: float, cy: float, r: float, **kwargs) -> drawsvg.Path:
    """Create a circle as a <path> element (LightBurn compatible)."""
    p = drawsvg.Path(**kwargs)
    p.M(cx - r, cy)
    p.A(r, r, 0, 1, 0, cx + r, cy)
    p.A(r, r, 0, 1, 0, cx - r, cy)
    p.Z()
    return p


def render_gerber_to_svg_string(cmd_buffer: ReadonlyCommandBuffer2) -> str:
    """Render command buffer to SVG string."""
    buffer = BytesIO()
    renderer = SvgRenderer2(
        SvgRenderer2Hooks(color_scheme=MANUFACTURING_SCHEME, scale=Decimal("1")),
    )
    output = renderer.render(cmd_buffer)
    output.save_to(buffer)
    return buffer.getvalue().decode('utf-8')


def get_outline_bounds(cmd_buffer: ReadonlyCommandBuffer2) -> tuple[float, float, float, float]:
    """Get min/max coordinates from command center-line points (no stroke inflation).

    Returns (min_x_mm, min_y_mm, max_x_mm, max_y_mm).
    """
    xs: list[float] = []
    ys: list[float] = []

    for cmd in cmd_buffer:
        if isinstance(cmd, (Line2, Arc2, CCArc2)):
            xs.append(float(cmd.start_point.x.as_millimeters()))
            ys.append(float(cmd.start_point.y.as_millimeters()))
            xs.append(float(cmd.end_point.x.as_millimeters()))
            ys.append(float(cmd.end_point.y.as_millimeters()))
        elif isinstance(cmd, Flash2):
            xs.append(float(cmd.flash_point.x.as_millimeters()))
            ys.append(float(cmd.flash_point.y.as_millimeters()))
        elif isinstance(cmd, Region2):
            for sub in cmd.command_buffer:
                if isinstance(sub, (Line2, Arc2, CCArc2)):
                    xs.append(float(sub.start_point.x.as_millimeters()))
                    ys.append(float(sub.start_point.y.as_millimeters()))
                    xs.append(float(sub.end_point.x.as_millimeters()))
                    ys.append(float(sub.end_point.y.as_millimeters()))

    return min(xs), min(ys), max(xs), max(ys)


def build_outline_path(cmd_buffer: ReadonlyCommandBuffer2, fill: str) -> drawsvg.Path:
    """Build a closed filled SVG path from edge cuts commands.

    Commands may be unordered; this chains them by matching start/end points.
    """
    def convert_y(y: Offset) -> float:
        return -(float(y.as_millimeters()))

    def convert_x(x: Offset) -> float:
        return float(x.as_millimeters())

    def point_key(pt) -> tuple[float, float]:
        return (round(float(pt.x.as_millimeters()), 4),
                round(float(pt.y.as_millimeters()), 4))

    # Collect drawable segments
    segments = [cmd for cmd in cmd_buffer if isinstance(cmd, (Line2, Arc2, CCArc2))]
    if not segments:
        return drawsvg.Path(fill=fill)

    # Chain segments by matching endpoints
    remaining = list(segments)
    chain = [remaining.pop(0)]

    while remaining:
        target = point_key(chain[-1].end_point)
        found = False
        for i, seg in enumerate(remaining):
            if point_key(seg.start_point) == target:
                chain.append(remaining.pop(i))
                found = True
                break
        if not found:
            break

    # Build SVG path from ordered chain
    path = drawsvg.Path(fill=fill)
    path.M(convert_x(chain[0].start_point.x), convert_y(chain[0].start_point.y))

    for cmd in chain:
        if isinstance(cmd, Line2):
            path.L(convert_x(cmd.end_point.x), convert_y(cmd.end_point.y))
        elif isinstance(cmd, (Arc2, CCArc2)):
            radius = float(cmd.get_radius().as_millimeters())
            relative_start = cmd.get_relative_start_point()
            relative_end = cmd.get_relative_end_point()
            angle_cw = relative_start.angle_between(relative_end)
            angle_ccw = relative_start.angle_between_cc(relative_end)
            if isinstance(cmd, Arc2):
                large_arc = angle_cw >= angle_ccw
                sweep = 1
            else:
                large_arc = not (angle_cw >= angle_ccw)
                sweep = 0
            path.A(radius, radius, 0, large_arc, sweep,
                   convert_x(cmd.end_point.x), convert_y(cmd.end_point.y))

    path.Z()
    return path


def linearize_arc(cmd, arc_step_deg: float = ARC_RESOLUTION_DEG) -> list[tuple[float, float]]:
    """Convert Arc2/CCArc2 to a list of (x, y) points in Gerber mm coords (Y-up)."""
    cx = float(cmd.center_point.x.as_millimeters())
    cy = float(cmd.center_point.y.as_millimeters())
    sx = float(cmd.start_point.x.as_millimeters())
    sy = float(cmd.start_point.y.as_millimeters())
    ex = float(cmd.end_point.x.as_millimeters())
    ey = float(cmd.end_point.y.as_millimeters())

    start_angle = math.atan2(sy - cy, sx - cx)
    end_angle = math.atan2(ey - cy, ex - cx)
    radius = math.hypot(sx - cx, sy - cy)

    if isinstance(cmd, Arc2):
        # Clockwise: angle decreasing
        sweep = start_angle - end_angle
        if sweep <= 0:
            sweep += 2 * math.pi
        n_segments = max(4, int(math.degrees(sweep) / arc_step_deg))
        points = []
        for i in range(n_segments + 1):
            a = start_angle - sweep * i / n_segments
            points.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    else:
        # Counter-clockwise: angle increasing
        sweep = end_angle - start_angle
        if sweep <= 0:
            sweep += 2 * math.pi
        n_segments = max(4, int(math.degrees(sweep) / arc_step_deg))
        points = []
        for i in range(n_segments + 1):
            a = start_angle + sweep * i / n_segments
            points.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))

    return points


def cmd_to_shapely(cmd, arc_step_deg: float = ARC_RESOLUTION_DEG):
    """Convert one pygerber command to a Shapely geometry in Gerber mm coords.

    Returns None for unsupported commands.
    """
    if isinstance(cmd, Line2):
        sx = float(cmd.start_point.x.as_millimeters())
        sy = float(cmd.start_point.y.as_millimeters())
        ex = float(cmd.end_point.x.as_millimeters())
        ey = float(cmd.end_point.y.as_millimeters())
        hw = get_stroke_width_safe(cmd.aperture) / 2
        line = LineString([(sx, sy), (ex, ey)])
        return line.buffer(hw, cap_style='round')

    elif isinstance(cmd, (Arc2, CCArc2)):
        pts = linearize_arc(cmd, arc_step_deg)
        hw = get_stroke_width_safe(cmd.aperture) / 2
        line = LineString(pts)
        return line.buffer(hw, cap_style='round')

    elif isinstance(cmd, Flash2):
        x = float(cmd.flash_point.x.as_millimeters())
        y = float(cmd.flash_point.y.as_millimeters())
        aperture = cmd.aperture

        geom = None
        if isinstance(aperture, Circle2):
            r = float(aperture.diameter.as_millimeters()) / 2
            geom = Point(x, y).buffer(r)
        elif isinstance(aperture, Obround2):
            # Obround = stadium shape: LineString along short axis buffered by half short dimension
            w = float(aperture.x_size.as_millimeters())
            h = float(aperture.y_size.as_millimeters())
            if w >= h:
                r = h / 2
                half_len = (w - h) / 2
                geom = LineString([(x - half_len, y), (x + half_len, y)]).buffer(r, cap_style='round')
            else:
                r = w / 2
                half_len = (h - w) / 2
                geom = LineString([(x, y - half_len), (x, y + half_len)]).buffer(r, cap_style='round')
        elif isinstance(aperture, Rectangle2):
            w = float(aperture.x_size.as_millimeters())
            h = float(aperture.y_size.as_millimeters())
            geom = box(x - w / 2, y - h / 2, x + w / 2, y + h / 2)
            rot = float(aperture.rotation)
            if rot != 0:
                geom = rotate(geom, rot, origin=(x, y))
        elif isinstance(aperture, Polygon2):
            r = float(aperture.outer_diameter.as_millimeters()) / 2
            n = aperture.number_vertices
            rot_deg = float(aperture.rotation)
            verts = []
            for i in range(n):
                angle = math.radians(rot_deg) + i * 2 * math.pi / n
                verts.append((x + r * math.cos(angle), y + r * math.sin(angle)))
            geom = Polygon(verts)
        elif isinstance(aperture, Macro2):
            macro_geom = commands_to_shapely(aperture.command_buffer, arc_step_deg)
            if macro_geom is not None and not macro_geom.is_empty:
                geom = translate(macro_geom, xoff=x, yoff=y)

        # Handle aperture hole
        if geom is not None and hasattr(aperture, 'hole_diameter') and aperture.hole_diameter is not None:
            hole_d = float(aperture.hole_diameter.as_millimeters())
            if hole_d > 0:
                geom = geom.difference(Point(x, y).buffer(hole_d / 2))

        return geom

    elif isinstance(cmd, Region2):
        coords = []
        for sub in cmd.command_buffer:
            if isinstance(sub, Line2):
                coords.append((
                    float(sub.start_point.x.as_millimeters()),
                    float(sub.start_point.y.as_millimeters()),
                ))
            elif isinstance(sub, (Arc2, CCArc2)):
                arc_pts = linearize_arc(sub, arc_step_deg)
                # Add all points except the last (which will be the next segment's start)
                coords.extend(arc_pts[:-1])
        # Add the last endpoint
        if cmd.command_buffer:
            last = None
            for sub in cmd.command_buffer:
                if isinstance(sub, (Line2, Arc2, CCArc2)):
                    last = sub
            if last is not None:
                coords.append((
                    float(last.end_point.x.as_millimeters()),
                    float(last.end_point.y.as_millimeters()),
                ))
        if len(coords) >= 3:
            return Polygon(coords)

    return None


def commands_to_shapely(cmd_buffer: ReadonlyCommandBuffer2, arc_step_deg: float = ARC_RESOLUTION_DEG):
    """Convert pygerber command buffer to a Shapely geometry, respecting polarity."""
    solid_geoms = []
    clear_geoms = []

    for cmd in cmd_buffer:
        geom = cmd_to_shapely(cmd, arc_step_deg)
        if geom is None or geom.is_empty:
            continue

        if cmd.transform.polarity.is_solid():
            solid_geoms.append(geom)
        else:
            clear_geoms.append(geom)

    if not solid_geoms:
        return Polygon()

    copper = make_valid(unary_union(solid_geoms))
    if clear_geoms:
        copper = copper.difference(make_valid(unary_union(clear_geoms)))

    return copper


@dataclass
class _ChainSeg:
    """A segment with a direction flag for chaining."""
    cmd: object
    reversed: bool
    arc_step_deg: float = ARC_RESOLUTION_DEG

    @property
    def start_key(self) -> tuple[float, float]:
        pt = self.cmd.end_point if self.reversed else self.cmd.start_point
        return (round(float(pt.x.as_millimeters()), 4),
                round(float(pt.y.as_millimeters()), 4))

    @property
    def end_key(self) -> tuple[float, float]:
        pt = self.cmd.start_point if self.reversed else self.cmd.end_point
        return (round(float(pt.x.as_millimeters()), 4),
                round(float(pt.y.as_millimeters()), 4))

    def to_coords(self) -> list[tuple[float, float]]:
        cmd = self.cmd
        if isinstance(cmd, Line2):
            if self.reversed:
                return [(float(cmd.end_point.x.as_millimeters()),
                         float(cmd.end_point.y.as_millimeters()))]
            else:
                return [(float(cmd.start_point.x.as_millimeters()),
                         float(cmd.start_point.y.as_millimeters()))]
        elif isinstance(cmd, (Arc2, CCArc2)):
            pts = linearize_arc(cmd, self.arc_step_deg)
            if self.reversed:
                pts = list(reversed(pts))
            return pts[:-1]  # exclude last (next segment's start)
        return []


def build_outline_polygon(cmd_buffer: ReadonlyCommandBuffer2, arc_step_deg: float = ARC_RESOLUTION_DEG) -> Polygon:
    """Build a Shapely Polygon from edge cuts commands.

    Commands may be unordered and in either direction; chains them by matching
    start/end points. Supports multiple contours (exterior + holes for cutouts).
    """
    def point_key(pt) -> tuple[float, float]:
        return (round(float(pt.x.as_millimeters()), 4),
                round(float(pt.y.as_millimeters()), 4))

    segments = [cmd for cmd in cmd_buffer if isinstance(cmd, (Line2, Arc2, CCArc2))]
    if not segments:
        return Polygon()

    # Chain segments into contours, allowing reversed segments
    remaining = list(segments)
    contours = []

    while remaining:
        chain: list[_ChainSeg] = [_ChainSeg(remaining.pop(0), False, arc_step_deg)]
        while remaining:
            target = chain[-1].end_key
            found = False
            for i, seg in enumerate(remaining):
                if point_key(seg.start_point) == target:
                    chain.append(_ChainSeg(remaining.pop(i), False, arc_step_deg))
                    found = True
                    break
                if point_key(seg.end_point) == target:
                    chain.append(_ChainSeg(remaining.pop(i), True, arc_step_deg))
                    found = True
                    break
            if not found:
                break

        # Convert chain to coordinate list
        coords = []
        for seg in chain:
            coords.extend(seg.to_coords())
        # Close with last endpoint
        last = chain[-1]
        end = last.end_key
        coords.append(end)

        if len(coords) >= 3:
            contours.append(Polygon(coords))

    if not contours:
        return Polygon()

    if len(contours) == 1:
        return make_valid(contours[0])

    # Largest contour = exterior, smaller ones = holes
    contours.sort(key=lambda p: p.area, reverse=True)
    exterior = contours[0]
    holes = contours[1:]
    result = exterior
    for hole in holes:
        result = result.difference(hole)
    return make_valid(result)


def shapely_to_svg_paths(geom, fill: str) -> list[drawsvg.Path]:
    """Convert a Shapely geometry to SVG <path> elements.

    Y-flip: svg_y = -gerber_y.
    """
    def ring_to_path_data(ring, path: drawsvg.Path):
        coords = list(ring.coords)
        if not coords:
            return
        path.M(coords[0][0], -coords[0][1])
        for x, y in coords[1:]:
            path.L(x, -y)
        path.Z()

    def polygon_to_path(poly: Polygon) -> drawsvg.Path:
        p = drawsvg.Path(fill=fill, fill_rule='evenodd')
        ring_to_path_data(poly.exterior, p)
        for interior in poly.interiors:
            ring_to_path_data(interior, p)
        return p

    paths = []

    if isinstance(geom, Polygon):
        if not geom.is_empty:
            paths.append(polygon_to_path(geom))
    elif isinstance(geom, MultiPolygon):
        for poly in geom.geoms:
            if not poly.is_empty:
                paths.append(polygon_to_path(poly))
    elif hasattr(geom, 'geoms'):
        # GeometryCollection — filter to only Polygons
        for g in geom.geoms:
            if isinstance(g, Polygon) and not g.is_empty:
                paths.append(polygon_to_path(g))

    return paths


def create_combined_svg(
    edge_cuts_buffer: ReadonlyCommandBuffer2,
    copper_buffer: ReadonlyCommandBuffer2,
    output_path: str,
    brim: float = 1.0,
    arc_step_deg: float = ARC_RESOLUTION_DEG,
) -> None:
    """Create combined SVG from edge cuts and copper layer using boolean geometry.

    Output: black = areas to etch (board minus copper), white = copper to keep.
    Each etch region is an independent <path fill="black"> for LightBurn compatibility.
    """
    # Use center-line outline of edge cuts for SVG dimensions (no stroke inflation)
    ol_min_x, ol_min_y, ol_max_x, ol_max_y = get_outline_bounds(edge_cuts_buffer)
    svg_width = (ol_max_x - ol_min_x) + 2 * brim
    svg_height = (ol_max_y - ol_min_y) + 2 * brim
    svg_min_x = ol_min_x - brim
    svg_min_y = ol_min_y - brim

    d = drawsvg.Drawing(
        width=svg_width,
        height=svg_height,
        origin=(svg_min_x, -svg_min_y - svg_height),
    )
    d.width = f"{svg_width}mm"
    d.height = f"{svg_height}mm"

    # White background (outside board = no laser)
    d.append(path_rect(
        svg_min_x, -svg_min_y - svg_height,
        svg_width, svg_height,
        fill='white',
    ))

    # Boolean geometry: etch = board - copper
    board = build_outline_polygon(edge_cuts_buffer, arc_step_deg)
    copper = commands_to_shapely(copper_buffer, arc_step_deg)

    board = make_valid(board)
    copper = make_valid(copper)

    # Clip copper to board outline, then subtract from board
    copper = copper.intersection(board)
    etch = board.difference(copper)

    # Output each etch polygon as a separate <path fill="black">
    for path_elem in shapely_to_svg_paths(etch, 'black'):
        d.append(path_elem)

    d.save_svg(output_path)


def render_commands_to_drawing(
    drawing: drawsvg.Drawing,
    cmd_buffer: ReadonlyCommandBuffer2,
    bbox: BoundingBox,
    color: str,
    clear_color: str = 'black',
    stroke_only: bool = False,
) -> None:
    """Render pygerber commands to drawsvg Drawing."""
    def convert_y(y: Offset) -> float:
        """Convert Gerber Y to SVG Y (flip axis)."""
        return -(float(y.as_millimeters()))

    def convert_x(x: Offset) -> float:
        """Convert Gerber X to SVG X."""
        return float(x.as_millimeters())

    def get_cmd_color(cmd) -> str:
        """Get fill color based on command polarity."""
        if cmd.transform.polarity.is_solid():
            return color
        return clear_color

    def compute_arc_params(cmd):
        """Compute SVG arc parameters (large_arc, sweep) from pygerber arc."""
        relative_start = cmd.get_relative_start_point()
        relative_end = cmd.get_relative_end_point()
        angle_cw = relative_start.angle_between(relative_end)
        angle_ccw = relative_start.angle_between_cc(relative_end)

        if isinstance(cmd, Arc2):
            large_arc = angle_cw >= angle_ccw
            sweep = 1
        else:  # CCArc2
            large_arc = not (angle_cw >= angle_ccw)
            sweep = 0

        return large_arc, sweep

    for cmd in cmd_buffer:
        if isinstance(cmd, Line2):
            x1 = convert_x(cmd.start_point.x)
            y1 = convert_y(cmd.start_point.y)
            x2 = convert_x(cmd.end_point.x)
            y2 = convert_y(cmd.end_point.y)
            cmd_color = get_cmd_color(cmd)

            if stroke_only:
                # Stroke-only: thin stroked path (LightBurn sets actual width)
                p = drawsvg.Path(stroke=cmd_color, stroke_width=0.1, fill='none')
                p.M(x1, y1)
                p.L(x2, y2)
                drawing.append(p)
            else:
                # Filled outline: rectangle body + semicircle endcaps
                hw = get_stroke_width_safe(cmd.aperture) / 2
                dx = x2 - x1
                dy = y2 - y1
                length = math.hypot(dx, dy)
                if length < 1e-9:
                    drawing.append(path_circle(x1, y1, hw, fill=cmd_color))
                else:
                    px = -dy / length * hw
                    py = dx / length * hw

                    p = drawsvg.Path(fill=cmd_color)
                    p.M(x1 + px, y1 + py)
                    p.A(hw, hw, 0, 0, 1, x1 - px, y1 - py)
                    p.L(x2 - px, y2 - py)
                    p.A(hw, hw, 0, 0, 1, x2 + px, y2 + py)
                    p.Z()
                    drawing.append(p)

        elif isinstance(cmd, (Arc2, CCArc2)):
            x1 = convert_x(cmd.start_point.x)
            y1 = convert_y(cmd.start_point.y)
            x2 = convert_x(cmd.end_point.x)
            y2 = convert_y(cmd.end_point.y)
            cmd_color = get_cmd_color(cmd)

            large_arc, sweep = compute_arc_params(cmd)
            radius = float(cmd.get_radius().as_millimeters())

            if stroke_only:
                # Stroke-only: thin stroked arc (LightBurn sets actual width)
                p = drawsvg.Path(stroke=cmd_color, stroke_width=0.1, fill='none')
                p.M(x1, y1)
                p.A(radius, radius, 0, large_arc, sweep, x2, y2)
                drawing.append(p)
            else:
                # Filled outline: outer/inner arc pair + semicircle endcaps
                cx_arc = convert_x(cmd.center_point.x)
                cy_arc = convert_y(cmd.center_point.y)
                hw = get_stroke_width_safe(cmd.aperture) / 2

                r_outer = radius + hw
                r_inner = max(radius - hw, 0)

                def radial_unit(px, py):
                    d = math.hypot(px - cx_arc, py - cy_arc)
                    if d < 1e-9:
                        return 1.0, 0.0
                    return (px - cx_arc) / d, (py - cy_arc) / d

                ux1, uy1 = radial_unit(x1, y1)
                ux2, uy2 = radial_unit(x2, y2)

                p = drawsvg.Path(fill=cmd_color)
                p.M(cx_arc + ux1 * r_outer, cy_arc + uy1 * r_outer)
                p.A(r_outer, r_outer, 0, large_arc, sweep,
                    cx_arc + ux2 * r_outer, cy_arc + uy2 * r_outer)
                p.A(hw, hw, 0, 0, 1,
                    cx_arc + ux2 * r_inner, cy_arc + uy2 * r_inner)
                p.A(r_inner, r_inner, 0, large_arc, 1 - sweep,
                    cx_arc + ux1 * r_inner, cy_arc + uy1 * r_inner)
                p.A(hw, hw, 0, 0, 1,
                    cx_arc + ux1 * r_outer, cy_arc + uy1 * r_outer)
                p.Z()
                drawing.append(p)

        elif isinstance(cmd, Flash2):
            x = convert_x(cmd.flash_point.x)
            y = convert_y(cmd.flash_point.y)
            aperture = cmd.aperture
            cmd_color = get_cmd_color(cmd)

            if isinstance(aperture, Circle2):
                diameter = float(aperture.diameter.as_millimeters())
                if stroke_only:
                    drawing.append(path_circle(
                        x, y, diameter / 2,
                        stroke=cmd_color, stroke_width=0.1, fill='none',
                    ))
                else:
                    drawing.append(path_circle(
                        x, y, diameter / 2,
                        fill=cmd_color,
                    ))
            elif isinstance(aperture, Obround2):
                # Must check before Rectangle2 since Obround2 extends Rectangle2
                width = float(aperture.x_size.as_millimeters())
                height = float(aperture.y_size.as_millimeters())
                rx = min(width, height) / 2
                if stroke_only:
                    drawing.append(path_rect(
                        x - width/2, y - height/2,
                        width, height, rx=rx,
                        stroke=cmd_color, stroke_width=0.1, fill='none',
                    ))
                else:
                    drawing.append(path_rect(
                        x - width/2, y - height/2,
                        width, height, rx=rx,
                        fill=cmd_color,
                    ))
            elif isinstance(aperture, Rectangle2):
                width = float(aperture.x_size.as_millimeters())
                height = float(aperture.y_size.as_millimeters())
                if stroke_only:
                    drawing.append(path_rect(
                        x - width/2, y - height/2,
                        width, height,
                        stroke=cmd_color, stroke_width=0.1, fill='none',
                    ))
                else:
                    drawing.append(path_rect(
                        x - width/2, y - height/2,
                        width, height,
                        fill=cmd_color,
                    ))
            elif isinstance(aperture, Polygon2):
                r = float(aperture.outer_diameter.as_millimeters()) / 2
                n = aperture.number_vertices
                rot = math.radians(float(aperture.rotation))
                p = drawsvg.Path(
                    fill='none' if stroke_only else cmd_color,
                    **(dict(stroke=cmd_color, stroke_width=0.1) if stroke_only else {}),
                )
                for i in range(n):
                    angle = rot + i * 2 * math.pi / n
                    vx = x + r * math.cos(angle)
                    vy = y - r * math.sin(angle)
                    if i == 0:
                        p.M(vx, vy)
                    else:
                        p.L(vx, vy)
                p.Z()
                drawing.append(p)
            elif isinstance(aperture, Macro2):
                # Render macro commands in a group translated to flash point
                g = drawsvg.Group(transform=f"translate({x},{y})")
                render_commands_to_drawing(
                    g, aperture.command_buffer, bbox, cmd_color, clear_color,
                )
                drawing.append(g)

        elif isinstance(cmd, Region2):
            if stroke_only or len(cmd.command_buffer) == 0:
                continue

            cmd_color = get_cmd_color(cmd)

            # Build a closed filled path from region boundary commands
            region_path = drawsvg.Path(fill=cmd_color)

            # Move to the start point of the first boundary command
            for sub_cmd in cmd.command_buffer:
                if isinstance(sub_cmd, (Line2, Arc2, CCArc2)):
                    region_path.M(
                        convert_x(sub_cmd.start_point.x),
                        convert_y(sub_cmd.start_point.y),
                    )
                    break

            # Trace the boundary with line-to and arc-to commands
            for sub_cmd in cmd.command_buffer:
                if isinstance(sub_cmd, Line2):
                    region_path.L(
                        convert_x(sub_cmd.end_point.x),
                        convert_y(sub_cmd.end_point.y),
                    )
                elif isinstance(sub_cmd, (Arc2, CCArc2)):
                    radius = float(sub_cmd.get_radius().as_millimeters())
                    large_arc, sweep = compute_arc_params(sub_cmd)
                    region_path.A(
                        radius, radius, 0,
                        large_arc, sweep,
                        convert_x(sub_cmd.end_point.x),
                        convert_y(sub_cmd.end_point.y),
                    )

            region_path.Z()
            drawing.append(region_path)


def create_cuts_svg(
    edge_cuts_buffer: ReadonlyCommandBuffer2,
    drill_holes: list[DrillHole],
    output_path: str,
    flip: bool = False,
    brim: float = 1.0,
) -> None:
    """Create SVG with edge cuts and drill holes."""
    bbox = get_bounding_box(edge_cuts_buffer)

    # Use center-line outline for SVG dimensions (no stroke inflation)
    ol_min_x, ol_min_y, ol_max_x, ol_max_y = get_outline_bounds(edge_cuts_buffer)
    pcb_min_x = ol_min_x
    pcb_max_x = ol_max_x

    # SVG dimensions with brim
    svg_width = (ol_max_x - ol_min_x) + 2 * brim
    svg_height = (ol_max_y - ol_min_y) + 2 * brim
    svg_min_x = ol_min_x - brim
    svg_min_y = ol_min_y - brim

    d = drawsvg.Drawing(
        width=svg_width,
        height=svg_height,
        origin=(svg_min_x, -svg_min_y - svg_height),
    )
    d.width = f"{svg_width}mm"
    d.height = f"{svg_height}mm"

    # Add white background
    d.append(path_rect(
        svg_min_x, -svg_min_y - svg_height,
        svg_width, svg_height,
        fill='white',
    ))

    # Render edge cuts
    render_commands_to_drawing(d, edge_cuts_buffer, bbox, 'black', stroke_only=True)

    # Render drill holes
    for hole in drill_holes:
        x = float(hole.x)
        y = -float(hole.y)  # Flip Y axis
        radius = float(hole.diameter) / 2

        if flip:
            x = pcb_max_x - (x - pcb_min_x)

        d.append(path_circle(
            x, y, radius,
            stroke='black', stroke_width=0.1, fill='none',
        ))

    d.save_svg(output_path)


def warn(msg: str) -> None:
    """Print a yellow warning message."""
    click.echo(click.style(f"⚠️  {msg}", fg='yellow'), err=True)


def info(msg: str) -> None:
    """Print a cyan info message."""
    click.echo(click.style(f"ℹ️  {msg}", fg='cyan'), err=True)


def success(msg: str) -> None:
    """Print a green success message."""
    click.echo(click.style(f"✅ {msg}", fg='green'), err=True)


def check_edge_cuts_filename(path: str) -> None:
    """Warn if file doesn't look like an edge cuts Gerber."""
    name = os.path.basename(path)
    if not name.lower().endswith('-edge_cuts.gbr'):
        warn(f"'{name}': expected *-Edge_Cuts.gbr suffix")


def check_copper_filename(path: str, flip: bool) -> None:
    """Warn if file doesn't look like a copper Gerber, or flip mismatch."""
    name = os.path.basename(path)
    base = name.lower()
    if not base.endswith('_cu.gbr'):
        warn(f"'{name}': expected *_Cu.gbr suffix")
    if base.endswith('-b_cu.gbr') and not flip:
        warn(f"'{name}': back copper file without --flip")
    if base.endswith('-f_cu.gbr') and flip:
        warn(f"'{name}': front copper file with --flip")


def check_silk_filename(path: str, flip: bool) -> None:
    """Warn if file doesn't look like a silkscreen Gerber, or flip mismatch."""
    name = os.path.basename(path)
    base = name.lower()
    if not base.endswith('_silkscreen.gbr'):
        warn(f"'{name}': expected *_Silkscreen.gbr suffix")
    if base.endswith('-b_silkscreen.gbr') and not flip:
        warn(f"'{name}': back silkscreen file without --flip")
    if base.endswith('-f_silkscreen.gbr') and flip:
        warn(f"'{name}': front silkscreen file with --flip")


@click.group()
def cli():
    """PCB Gerber to SVG converter for manufacturing."""
    pass


@cli.command()
@click.argument('edge_cuts', type=click.Path(exists=True))
@click.argument('copper', type=click.Path(exists=True))
@click.option('--flip', is_flag=True, help='Flip on X axis for bottom layer')
@click.option('--brim', default=1.0, type=float, help='Brim width in mm (default: 1)')
@click.option('-o', '--output', default='copper.svg', help='Output SVG file')
@click.option('--linearization-step', default=1.0, type=float, help='Arc linearization step in degrees (default: 1)')
def copper(edge_cuts: str, copper: str, flip: bool, brim: float, output: str, linearization_step: float):
    """Generate copper layer SVG from edge cuts and copper Gerber files.

    Output is inverted: black = areas to remove/etch, white = copper to keep.
    """
    check_edge_cuts_filename(edge_cuts)
    check_copper_filename(copper, flip)

    info(f"📐 Edge cuts: {edge_cuts}")
    info(f"🔌 Copper:    {copper}")
    info(f"🔄 Flip: {'yes' if flip else 'no'} | 📏 Brim: {brim}mm")

    edge_cuts_buffer = parse_gerber(edge_cuts)
    copper_buffer = parse_gerber(copper)

    if flip:
        edge_cuts_buffer = apply_flip(edge_cuts_buffer, True)
        copper_buffer = apply_flip(copper_buffer, True)

    create_combined_svg(edge_cuts_buffer, copper_buffer, output, brim=brim, arc_step_deg=linearization_step)

    ol = get_outline_bounds(edge_cuts_buffer)
    pcb_w, pcb_h = ol[2] - ol[0], ol[3] - ol[1]
    svg_w, svg_h = pcb_w + 2 * brim, pcb_h + 2 * brim
    success(f"Created {output} ({svg_w}mm × {svg_h}mm, PCB {pcb_w}mm × {pcb_h}mm)")


@cli.command()
@click.argument('edge_cuts', type=click.Path(exists=True))
@click.argument('drills', nargs=-1, type=click.Path(exists=True))
@click.option('--flip', is_flag=True, help='Flip on X axis for bottom layer')
@click.option('--brim', default=1.0, type=float, help='Brim width in mm (default: 1)')
@click.option('-o', '--output', default='cuts.svg', help='Output SVG file')
def cuts(edge_cuts: str, drills: tuple, flip: bool, brim: float, output: str):
    """Generate holes/edge cuts SVG from edge cuts Gerber and drill files.

    Output shows board outline and drill holes (for LightBurn).
    """
    check_edge_cuts_filename(edge_cuts)

    info(f"📐 Edge cuts: {edge_cuts}")
    for drl in drills:
        info(f"🕳️  Drill:     {drl}")
    info(f"🔄 Flip: {'yes' if flip else 'no'} | 📏 Brim: {brim}mm")

    edge_cuts_buffer = parse_gerber(edge_cuts)

    if flip:
        edge_cuts_buffer = apply_flip(edge_cuts_buffer, True)

    all_holes: list[DrillHole] = []
    for drl_file in drills:
        all_holes.extend(parse_excellon(drl_file))

    create_cuts_svg(edge_cuts_buffer, all_holes, output, flip=flip, brim=brim)

    ol = get_outline_bounds(edge_cuts_buffer)
    pcb_w, pcb_h = ol[2] - ol[0], ol[3] - ol[1]
    svg_w, svg_h = pcb_w + 2 * brim, pcb_h + 2 * brim
    success(f"Created {output} ({svg_w}mm × {svg_h}mm, PCB {pcb_w}mm × {pcb_h}mm, {len(all_holes)} holes)")


def create_single_layer_svg(
    cmd_buffer: ReadonlyCommandBuffer2,
    output_path: str,
    brim: float = 1.0,
    edge_cuts_buffer: Optional[ReadonlyCommandBuffer2] = None,
    arc_step_deg: float = ARC_RESOLUTION_DEG,
) -> None:
    """Create SVG from a single Gerber layer using boolean geometry.

    Each region is an independent <path fill="black"> for LightBurn compatibility.
    """
    # Use edge cuts outline for sizing if provided, otherwise use layer bounds
    size_buffer = edge_cuts_buffer if edge_cuts_buffer is not None else cmd_buffer
    ol_min_x, ol_min_y, ol_max_x, ol_max_y = get_outline_bounds(size_buffer)

    # SVG dimensions with brim
    svg_width = (ol_max_x - ol_min_x) + 2 * brim
    svg_height = (ol_max_y - ol_min_y) + 2 * brim
    svg_min_x = ol_min_x - brim
    svg_min_y = ol_min_y - brim

    d = drawsvg.Drawing(
        width=svg_width,
        height=svg_height,
        origin=(svg_min_x, -svg_min_y - svg_height),
    )
    d.width = f"{svg_width}mm"
    d.height = f"{svg_height}mm"

    # Add white background
    d.append(path_rect(
        svg_min_x, -svg_min_y - svg_height,
        svg_width, svg_height,
        fill='white',
    ))

    # Convert layer to Shapely geometry and output as paths
    layer_geom = commands_to_shapely(cmd_buffer, arc_step_deg)
    layer_geom = make_valid(layer_geom)

    # Clip to board outline if edge cuts provided
    if edge_cuts_buffer is not None:
        board = build_outline_polygon(edge_cuts_buffer, arc_step_deg)
        board = make_valid(board)
        layer_geom = layer_geom.intersection(board)

    for path_elem in shapely_to_svg_paths(layer_geom, 'black'):
        d.append(path_elem)

    d.save_svg(output_path)


@cli.command()
@click.argument('edge_cuts', type=click.Path(exists=True))
@click.argument('silkscreen', type=click.Path(exists=True))
@click.option('--flip', is_flag=True, help='Flip on X axis for bottom layer')
@click.option('--brim', default=1.0, type=float, help='Brim width in mm (default: 1)')
@click.option('-o', '--output', default='silk.svg', help='Output SVG file')
@click.option('--linearization-step', default=1.0, type=float, help='Arc linearization step in degrees (default: 1)')
def silk(edge_cuts: str, silkscreen: str, flip: bool, brim: float, output: str, linearization_step: float):
    """Generate silkscreen SVG from edge cuts and silkscreen Gerber files.

    Output is black where silkscreen exists, white elsewhere (for LightBurn).
    SVG size is determined by edge cuts outline.
    """
    check_edge_cuts_filename(edge_cuts)
    check_silk_filename(silkscreen, flip)

    info(f"📐 Edge cuts:   {edge_cuts}")
    info(f"🖌️  Silkscreen: {silkscreen}")
    info(f"🔄 Flip: {'yes' if flip else 'no'} | 📏 Brim: {brim}mm")

    edge_cuts_buffer = parse_gerber(edge_cuts)
    silk_buffer = parse_gerber(silkscreen)

    if flip:
        edge_cuts_buffer = apply_flip(edge_cuts_buffer, True)
        silk_buffer = apply_flip(silk_buffer, True)

    create_single_layer_svg(silk_buffer, output, brim=brim, edge_cuts_buffer=edge_cuts_buffer, arc_step_deg=linearization_step)

    ol = get_outline_bounds(edge_cuts_buffer)
    pcb_w, pcb_h = ol[2] - ol[0], ol[3] - ol[1]
    svg_w, svg_h = pcb_w + 2 * brim, pcb_h + 2 * brim
    success(f"Created {output} ({svg_w}mm × {svg_h}mm, PCB {pcb_w}mm × {pcb_h}mm)")


def find_kicad_file(directory: str, suffix: str) -> Optional[str]:
    """Find a file in directory ending with suffix (case-insensitive)."""
    suffix_lower = suffix.lower()
    for name in os.listdir(directory):
        if name.lower().endswith(suffix_lower):
            return os.path.join(directory, name)
    return None


@cli.command()
@click.argument('directory', type=click.Path(exists=True, file_okay=False))
@click.option('--brim', default=1.0, type=float, help='Brim width in mm (default: 1)')
@click.option('-o', '--output-dir', default=None, help='Output directory (default: <directory>/jcz-manufacture)')
@click.option('--linearization-step', default=1.0, type=float, help='Arc linearization step in degrees (default: 1)')
def kicad(directory: str, brim: float, output_dir: Optional[str], linearization_step: float):
    """Auto-convert a KiCad Gerber export directory.

    Scans DIRECTORY for Edge_Cuts, F/B_Cu, F/B_Silkscreen, PTH/NPTH drill files
    and generates all applicable SVGs into jcz-manufacture/ subdirectory.
    """
    directory = os.path.abspath(directory)
    if output_dir is None:
        output_dir = os.path.join(directory, 'jcz-manufacture')

    info(f"📂 Scanning: {directory}")
    info(f"📏 Brim: {brim}mm")

    # Find files
    edge_cuts_path = find_kicad_file(directory, '-Edge_Cuts.gbr')
    f_cu_path = find_kicad_file(directory, '-F_Cu.gbr')
    b_cu_path = find_kicad_file(directory, '-B_Cu.gbr')
    f_silk_path = find_kicad_file(directory, '-F_Silkscreen.gbr')
    b_silk_path = find_kicad_file(directory, '-B_Silkscreen.gbr')
    pth_path = find_kicad_file(directory, '-PTH.drl')
    npth_path = find_kicad_file(directory, '-NPTH.drl')

    # Log found files
    found_files = {
        '📐 Edge Cuts': edge_cuts_path,
        '🔌 Front Copper': f_cu_path,
        '🔌 Back Copper': b_cu_path,
        '🖌️  Front Silk': f_silk_path,
        '🖌️  Back Silk': b_silk_path,
        '🕳️  PTH Drill': pth_path,
        '🕳️  NPTH Drill': npth_path,
    }
    for label, path in found_files.items():
        if path:
            info(f"{label}: {os.path.basename(path)}")
        else:
            click.echo(click.style(f"   {label}: not found", fg='bright_black'), err=True)

    if not edge_cuts_path:
        click.echo(click.style("❌ No Edge_Cuts file found — cannot proceed", fg='red'), err=True)
        raise SystemExit(1)

    # Parse edge cuts (shared by all outputs)
    edge_cuts_buffer = parse_gerber(edge_cuts_path)
    ol = get_outline_bounds(edge_cuts_buffer)
    pcb_w, pcb_h = ol[2] - ol[0], ol[3] - ol[1]
    svg_w, svg_h = pcb_w + 2 * brim, pcb_h + 2 * brim

    info(f"📐 PCB size: {pcb_w}mm × {pcb_h}mm → SVG: {svg_w}mm × {svg_h}mm")

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    click.echo("", err=True)
    info(f"📁 Output: {output_dir}")

    created = []

    # Copper top
    if f_cu_path:
        out = os.path.join(output_dir, 'copper_top.svg')
        copper_buffer = parse_gerber(f_cu_path)
        create_combined_svg(edge_cuts_buffer, copper_buffer, out, brim=brim, arc_step_deg=linearization_step)
        success(f"Created {out}")
        created.append(out)

    # Copper bottom
    if b_cu_path:
        out = os.path.join(output_dir, 'copper_bottom.svg')
        ec_flip = apply_flip(edge_cuts_buffer, True)
        copper_buffer = apply_flip(parse_gerber(b_cu_path), True)
        create_combined_svg(ec_flip, copper_buffer, out, brim=brim, arc_step_deg=linearization_step)
        success(f"Created {out}")
        created.append(out)

    # Silkscreen top
    if f_silk_path:
        out = os.path.join(output_dir, 'silk_top.svg')
        silk_buffer = parse_gerber(f_silk_path)
        create_single_layer_svg(silk_buffer, out, brim=brim, edge_cuts_buffer=edge_cuts_buffer, arc_step_deg=linearization_step)
        success(f"Created {out}")
        created.append(out)

    # Silkscreen bottom
    if b_silk_path:
        out = os.path.join(output_dir, 'silk_bottom.svg')
        ec_flip = apply_flip(edge_cuts_buffer, True)
        silk_buffer = apply_flip(parse_gerber(b_silk_path), True)
        create_single_layer_svg(silk_buffer, out, brim=brim, edge_cuts_buffer=ec_flip, arc_step_deg=linearization_step)
        success(f"Created {out}")
        created.append(out)

    # Cuts (edge cuts + drill holes)
    # If only back copper (no front), flip cuts to match the bottom-side workflow
    drill_paths = [p for p in [pth_path, npth_path] if p]
    flip_cuts = b_cu_path is not None and f_cu_path is None
    cuts_ec = apply_flip(edge_cuts_buffer, True) if flip_cuts else edge_cuts_buffer
    cuts_name = 'cuts_flipped.svg' if flip_cuts else 'cuts.svg'
    out = os.path.join(output_dir, cuts_name)
    all_holes: list[DrillHole] = []
    for drl_file in drill_paths:
        all_holes.extend(parse_excellon(drl_file))
    create_cuts_svg(cuts_ec, all_holes, out, flip=flip_cuts, brim=brim)
    if flip_cuts:
        info("🔄 Cuts flipped to match bottom-side copper")
    success(f"Created {out} ({len(all_holes)} holes)")
    created.append(out)

    click.echo("", err=True)
    success(f"Done — {len(created)} files in {output_dir}")


if __name__ == '__main__':
    cli()
