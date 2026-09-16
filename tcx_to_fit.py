#!/usr/bin/env python3
"""Convert Huawei Health TCX exports to standard Garmin FIT activity files.

Install the only dependency first:
    python -m pip install garmin-fit-sdk

Examples:
    python tcx_to_fit.py "20260915户外骑行.tcx"
    python tcx_to_fit.py "C:\\Huawei\\TCX" --output "C:\\Huawei\\FIT"
"""

from __future__ import annotations

import argparse
import math
import sys
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    from garmin_fit_sdk import Decoder, Encoder, FIT_EPOCH_S, Profile, Stream
except ImportError:  # Keep the error actionable when the script is copied by itself.
    sys.exit(
        "Missing dependency: garmin-fit-sdk\n"
        "Install it with: python -m pip install garmin-fit-sdk"
    )


SEMICIRCLES_PER_DEGREE = (2**31) / 180.0
MAX_GPS_STEP_METERS = 2_000.0  # Ignore obvious GPS jumps when calculating distance.


@dataclass
class TrackPoint:
    timestamp: datetime
    latitude: Optional[float]
    longitude: Optional[float]
    altitude: Optional[float]
    heart_rate: Optional[int]
    cadence: Optional[int]
    source_distance: Optional[float]
    source_speed: Optional[float]
    distance: float = 0.0
    speed: Optional[float] = None


@dataclass
class LapData:
    points: list[TrackPoint]
    reported_distance: Optional[float]
    reported_elapsed: Optional[float]


def local_name(element: ET.Element) -> str:
    """Return an XML tag name without a namespace."""
    return element.tag.rsplit("}", 1)[-1]


def first_descendant(element: ET.Element, name: str) -> Optional[ET.Element]:
    for item in element.iter():
        if local_name(item) == name:
            return item
    return None


def text_at(element: ET.Element, name: str) -> Optional[str]:
    item = first_descendant(element, name)
    if item is None or item.text is None:
        return None
    value = item.text.strip()
    return value or None


def number_at(element: ET.Element, name: str) -> Optional[float]:
    value = text_at(element, name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_time(value: str) -> datetime:
    """Parse TCX timestamps, including Huawei's trailing Z form."""
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_uint8(value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    return max(0, min(255, round(value)))


def parse_trackpoint(element: ET.Element) -> Optional[TrackPoint]:
    time_text = text_at(element, "Time")
    if time_text is None:
        return None

    position = first_descendant(element, "Position")
    latitude = number_at(position, "LatitudeDegrees") if position is not None else None
    longitude = number_at(position, "LongitudeDegrees") if position is not None else None

    # Common TCX fields plus Garmin ActivityExtension aliases used by some exporters.
    speed = number_at(element, "Speed")
    if speed is None:
        speed = number_at(element, "EnhancedSpeed")

    cadence = number_at(element, "Cadence")
    if cadence is None:
        cadence = number_at(element, "RunCadence")

    heart_rate_element = first_descendant(element, "HeartRateBpm")
    heart_rate = number_at(heart_rate_element, "Value") if heart_rate_element is not None else None

    return TrackPoint(
        timestamp=parse_time(time_text),
        latitude=latitude,
        longitude=longitude,
        altitude=number_at(element, "AltitudeMeters"),
        heart_rate=to_uint8(heart_rate),
        cadence=to_uint8(cadence),
        source_distance=number_at(element, "DistanceMeters"),
        source_speed=speed,
    )


def parse_tcx(path: Path) -> tuple[list[LapData], str]:
    """Read TCX without relying on its namespace or exact Huawei schema variant."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Invalid TCX XML: {exc}") from exc

    activities = [item for item in root.iter() if local_name(item) == "Activity"]
    if not activities:
        raise ValueError("No Activity element was found in this TCX file.")

    sport = activities[0].attrib.get("Sport", "Cycling")
    laps: list[LapData] = []
    for activity in activities:
        for lap in (item for item in activity if local_name(item) == "Lap"):
            points = [
                parsed
                for item in lap.iter()
                if local_name(item) == "Trackpoint"
                for parsed in [parse_trackpoint(item)]
                if parsed is not None
            ]
            if points:
                points.sort(key=lambda point: point.timestamp)
                laps.append(
                    LapData(
                        points,
                        number_at(lap, "DistanceMeters"),
                        number_at(lap, "TotalTimeSeconds"),
                    )
                )

    if not laps:
        raise ValueError("No timestamped Trackpoint elements were found in this TCX file.")
    return laps, sport


def haversine_meters(a: TrackPoint, b: TrackPoint) -> float:
    if None in (a.latitude, a.longitude, b.latitude, b.longitude):
        return 0.0
    lat1, lon1 = math.radians(a.latitude), math.radians(a.longitude)
    lat2, lon2 = math.radians(b.latitude), math.radians(b.longitude)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    sin_lat, sin_lon = math.sin(dlat / 2), math.sin(dlon / 2)
    return 6_371_008.8 * 2 * math.asin(
        min(1.0, math.sqrt(sin_lat * sin_lat + math.cos(lat1) * math.cos(lat2) * sin_lon * sin_lon))
    )


def populate_distances_and_speeds(laps: list[LapData]) -> None:
    """Use TCX values when present; otherwise calculate from adjacent GPS points.

    Huawei's export sample has a lap-level distance but no per-point distance.  In
    that situation the calculated point distances are proportionally scaled to the
    lap total, preserving the distance that Huawei reports for the activity.
    """
    activity_offset = 0.0
    for lap in laps:
        points = lap.points
        first_source_distance = next((p.source_distance for p in points if p.source_distance is not None), None)
        last_positioned: Optional[TrackPoint] = None
        calculated = 0.0
        raw_distances: list[float] = []

        for point in points:
            if point.source_distance is not None and first_source_distance is not None:
                raw_distance = max(0.0, point.source_distance - first_source_distance)
            else:
                step = haversine_meters(last_positioned, point) if last_positioned else 0.0
                if step > MAX_GPS_STEP_METERS:
                    step = 0.0
                calculated += step
                raw_distance = calculated
            raw_distances.append(raw_distance)
            if point.latitude is not None and point.longitude is not None:
                last_positioned = point

        calculated_total = raw_distances[-1] if raw_distances else 0.0
        target_total = lap.reported_distance
        if target_total is not None and calculated_total > 0 and first_source_distance is None:
            scale = target_total / calculated_total
        else:
            scale = 1.0

        for point, raw_distance in zip(points, raw_distances):
            point.distance = activity_offset + raw_distance * scale
        activity_offset = points[-1].distance

        for index, point in enumerate(points):
            if point.source_speed is not None and point.source_speed >= 0:
                point.speed = point.source_speed
            elif index:
                previous = points[index - 1]
                elapsed = (point.timestamp - previous.timestamp).total_seconds()
                point.speed = (point.distance - previous.distance) / elapsed if elapsed > 0 else previous.speed
            else:
                point.speed = 0.0


def fit_timestamp(value: datetime) -> int:
    return int(value.timestamp()) - FIT_EPOCH_S


def fit_sport(tcx_sport: str) -> str:
    normalized = tcx_sport.lower()
    if "run" in normalized or "跑" in tcx_sport:
        return "running"
    if "swim" in normalized or "游" in tcx_sport:
        return "swimming"
    # Huawei labels cycling activities in Chinese (for example, 户外骑行).
    return "cycling"


def ascent_descent(points: Iterable[TrackPoint]) -> tuple[float, float]:
    ascent = descent = 0.0
    previous: Optional[float] = None
    for point in points:
        if point.altitude is None:
            continue
        if previous is not None:
            change = point.altitude - previous
            if change > 0:
                ascent += change
            else:
                descent -= change
        previous = point.altitude
    return ascent, descent


def write_fit(laps: list[LapData], tcx_sport: str, source: Path, destination: Path) -> None:
    populate_distances_and_speeds(laps)
    all_points = [point for lap in laps for point in lap.points]
    start, end = all_points[0].timestamp, all_points[-1].timestamp
    start_fit, end_fit = fit_timestamp(start), fit_timestamp(end)
    serial = zlib.crc32(source.read_bytes()) & 0xFFFFFFFF
    serial = serial or 1
    sport = fit_sport(tcx_sport)

    encoder = Encoder()
    write = encoder.write_mesg
    local_utc_offset = int(datetime.now().astimezone().utcoffset().total_seconds())
    write({
        "mesg_num": Profile["mesg_num"]["FILE_ID"],
        "type": "activity",
        "manufacturer": "development",
        "product": 0,
        "serial_number": serial,
        "time_created": start_fit,
    })
    write({
        "mesg_num": Profile["mesg_num"]["DEVICE_INFO"],
        "timestamp": start_fit,
        "device_index": "creator",
        "manufacturer": "development",
        "product": 0,
        "serial_number": serial,
        "product_name": "Huawei Health TCX converter",
        "software_version": 1.0,
    })
    write({
        "mesg_num": Profile["mesg_num"]["EVENT"],
        "timestamp": start_fit,
        "event": "timer",
        "event_type": "start",
    })

    for point in all_points:
        record = {
            "mesg_num": Profile["mesg_num"]["RECORD"],
            "timestamp": fit_timestamp(point.timestamp),
            "distance": point.distance,
        }
        if point.latitude is not None and point.longitude is not None:
            record["position_lat"] = round(point.latitude * SEMICIRCLES_PER_DEGREE)
            record["position_long"] = round(point.longitude * SEMICIRCLES_PER_DEGREE)
        if point.altitude is not None:
            record["enhanced_altitude"] = point.altitude
        if point.speed is not None:
            record["enhanced_speed"] = max(0.0, point.speed)
        if point.heart_rate is not None:
            record["heart_rate"] = point.heart_rate
        if point.cadence is not None:
            record["cadence"] = point.cadence
        write(record)

    write({
        "mesg_num": Profile["mesg_num"]["EVENT"],
        "timestamp": end_fit,
        "event": "timer",
        "event_type": "stop",
    })

    for index, lap in enumerate(laps):
        lap_start, lap_end = lap.points[0], lap.points[-1]
        lap_ascent, lap_descent = ascent_descent(lap.points)
        elapsed = lap.reported_elapsed
        if elapsed is None:
            elapsed = max(0.0, (lap_end.timestamp - lap_start.timestamp).total_seconds())
        write({
            "mesg_num": Profile["mesg_num"]["LAP"],
            "message_index": index,
            "timestamp": fit_timestamp(lap_end.timestamp),
            "start_time": fit_timestamp(lap_start.timestamp),
            "total_elapsed_time": elapsed,
            "total_timer_time": elapsed,
            "total_distance": lap_end.distance - lap_start.distance,
            "total_ascent": round(lap_ascent),
            "total_descent": round(lap_descent),
        })

    total_ascent, total_descent = ascent_descent(all_points)
    elapsed = (
        sum(lap.reported_elapsed for lap in laps)
        if all(lap.reported_elapsed is not None for lap in laps)
        else max(0.0, (end - start).total_seconds())
    )
    total_distance = all_points[-1].distance - all_points[0].distance
    write({
        "mesg_num": Profile["mesg_num"]["SESSION"],
        "message_index": 0,
        "timestamp": end_fit,
        "start_time": start_fit,
        "total_elapsed_time": elapsed,
        "total_timer_time": elapsed,
        "total_distance": total_distance,
        "total_ascent": round(total_ascent),
        "total_descent": round(total_descent),
        "sport": sport,
        "sub_sport": "generic",
        "first_lap_index": 0,
        "num_laps": len(laps),
    })
    write({
        "mesg_num": Profile["mesg_num"]["ACTIVITY"],
        "timestamp": end_fit,
        "num_sessions": 1,
        "local_timestamp": end_fit + local_utc_offset,
        "total_timer_time": elapsed,
    })

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoder.close())


def validate_fit(path: Path) -> None:
    """Verify FIT header, CRC, decoder result, and required activity messages."""
    if path.stat().st_size < 16:
        raise ValueError("Generated FIT file is unexpectedly small.")
    if not Decoder.is_fit(Stream.from_file(str(path))):
        raise ValueError("Generated file does not have a valid FIT header.")

    required = {
        Profile["mesg_num"]["FILE_ID"],
        Profile["mesg_num"]["RECORD"],
        Profile["mesg_num"]["LAP"],
        Profile["mesg_num"]["SESSION"],
        Profile["mesg_num"]["ACTIVITY"],
    }
    seen: set[int] = set()

    def listener(mesg_num: int, _message: dict) -> None:
        seen.add(mesg_num)

    _messages, errors = Decoder(Stream.from_file(str(path))).read(mesg_listener=listener)
    if errors:
        raise ValueError(f"FIT decoder reported errors: {errors}")
    missing = required - seen
    if missing:
        raise ValueError(f"FIT is missing required message types: {sorted(missing)}")


def convert_one(source: Path, destination: Path, validate: bool) -> tuple[int, float]:
    laps, sport = parse_tcx(source)
    write_fit(laps, sport, source, destination)
    if validate:
        validate_fit(destination)
    points = sum(len(lap.points) for lap in laps)
    distance = laps[-1].points[-1].distance - laps[0].points[0].distance
    return points, distance


def output_path_for(source: Path, input_path: Path, output: Optional[Path]) -> Path:
    if input_path.is_file():
        if output is None:
            return source.with_suffix(".fit")
        return output / source.with_suffix(".fit").name if output.is_dir() else output
    output_root = output if output is not None else input_path / "fit"
    return output_root / source.relative_to(input_path).with_suffix(".fit")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="A .tcx file or a folder containing .tcx files")
    parser.add_argument("--output", "-o", type=Path, help="Output .fit file (single input) or output folder (batch)")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output file")
    parser.add_argument("--no-validate", action="store_true", help="Skip FIT decode/CRC validation after writing")
    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    if not input_path.exists():
        parser.error(f"Input does not exist: {input_path}")
    if input_path.is_file() and input_path.suffix.lower() != ".tcx":
        parser.error("Input file must have a .tcx extension.")

    sources = [input_path] if input_path.is_file() else sorted(
        path for path in input_path.rglob("*") if path.is_file() and path.suffix.lower() == ".tcx"
    )
    if not sources:
        parser.error("No .tcx files were found.")

    failures = 0
    for source in sources:
        destination = output_path_for(source, input_path, args.output)
        if destination.exists() and not args.overwrite:
            print(f"SKIP  {source.name} (output already exists: {destination})")
            continue
        try:
            points, distance = convert_one(source, destination, validate=not args.no_validate)
            print(f"OK    {source.name} -> {destination} ({points} points, {distance / 1000:.2f} km)")
        except Exception as exc:  # Continue in batch mode and clearly report every failed file.
            failures += 1
            print(f"FAIL  {source}: {exc}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
