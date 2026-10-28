#!/usr/bin/env python3
"""Interactive solar orientation viewer. Python 3 + tkinter + astropy + matplotlib."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import tkinter as tk
import threading
import urllib.request
import urllib.parse
import re
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image
from tkinter import ttk, messagebox, filedialog

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, get_sun
from astropy.time import Time
from astropy.utils import iers
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.patches import Circle
from matplotlib.transforms import Affine2D

VISIBLE_SOLAR_DISK_URL = "https://www.spaceweatherlive.com/images/SDO/SDO_HMIIF_1024.jpg"
SOURCE_PAGE = "https://www.spaceweatherlive.com/es/actividad-solar/regiones-de-manchas-solares.html"
THESKYLIVE_PAGE = "https://theskylive.com/sun-info"

# IAU solar rotation pole in J2000/ICRS equatorial coordinates.
SOLAR_POLE_RA = math.radians(286.13)
SOLAR_POLE_DEC = math.radians(63.87)
POLE_VECTOR = np.array([
    math.cos(SOLAR_POLE_DEC) * math.cos(SOLAR_POLE_RA),
    math.cos(SOLAR_POLE_DEC) * math.sin(SOLAR_POLE_RA),
    math.sin(SOLAR_POLE_DEC),
])
# Avoid network access at runtime; Astropy uses bundled IERS tables.
iers.conf.auto_download = False


def compute_sun(lat: float, lon: float, elevation: float, utc_string: str) -> dict:
    """Return apparent GCRS solar coordinates and unrefracted horizontal coordinates."""
    t = Time(datetime.strptime(utc_string, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
    site = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=elevation * u.m)
    sun = get_sun(t)  # Apparent geocentric RA/Dec in GCRS
    horizontal = sun.transform_to(AltAz(obstime=t, location=site, pressure=0 * u.hPa))
    ra = sun.ra.to_value(u.rad)
    dec = sun.dec.to_value(u.rad)
    towards_sun = np.array([math.cos(dec) * math.cos(ra),
                            math.cos(dec) * math.sin(ra), math.sin(dec)])
    towards_east = np.array([-math.sin(ra), math.cos(ra), 0.0])
    towards_north = np.array([-math.sin(dec) * math.cos(ra),
                              -math.sin(dec) * math.sin(ra), math.cos(dec)])
    # P: solar north position angle measured eastward from celestial north.
    p_angle = math.atan2(np.dot(POLE_VECTOR, towards_east),
                         np.dot(POLE_VECTOR, towards_north))
    # B0: heliographic latitude at disc centre; observer direction is -towards_sun.
    b0 = math.asin(float(np.clip(-np.dot(POLE_VECTOR, towards_sun), -1., 1.)))
    return {"time": t, "ra": sun.ra, "dec": sun.dec,
            "alt": horizontal.alt.deg, "az": horizontal.az.deg,
            "P": math.degrees(p_angle), "B0": math.degrees(b0)}


class SolarOrientationApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Solar Orientation — SkyWatcher ED80")
        self.geometry("1160x800")
        self.minsize(920, 660)
        self.configure(bg="#edf2f7")
        self.entries = {}
        self.view = tk.StringVar(value="sky")
        self.rotation = tk.StringVar(value="0")
        self.data = None
        self.solar_image = None
        self.active_regions = []
        self.show_region_labels = tk.BooleanVar(value=True)
        self.show_solar_image = tk.BooleanVar(value=False)
        self.image_status = tk.StringVar(value="Image not loaded")
        self.image_busy = False
        self.image_loaded_at = None
        self.image_source = "SpaceWeatherLive"
        self.theskylive_image = None
        self.spaceweather_image = None
        self.selected_image = tk.StringVar(value="spaceweather")
        # The original JPEG orientation cannot be inferred from an uncalibrated JPEG.
        # Default assumption: both NASA/SDO HMI products are solar-north-up.
        self.source_orientation = {
            "spaceweather": tk.StringVar(value="solar"),
            "theskylive": tk.StringVar(value="solar"),
        }
        self.source_offset = {
            "spaceweather": tk.StringVar(value="0"),
            "theskylive": tk.StringVar(value="0"),
        }
        self.use_photo_epoch = tk.BooleanVar(value=True)
        self.photo_epochs = {}
        self.create_widgets()
        self.calculate()

    def add_entry(self, parent, key, title, initial, width=12):
        row = ttk.Frame(parent)
        row.pack(side="left", padx=(0, 12), pady=5)
        ttk.Label(row, text=title).pack(anchor="w")
        entry = ttk.Entry(row, width=width)
        entry.insert(0, initial)
        entry.pack()
        self.entries[key] = entry

    def create_widgets(self):
        top = ttk.Frame(self, padding=12)
        top.pack(side="top", fill="x")
        ttk.Label(top, text="Solar orientation and ephemerides", font=("Arial", 18, "bold")).pack(anchor="w")
        ttk.Label(top, text="Geographic coordinates in decimal degrees · North/East positive · UTC date/time",
                  foreground="#455469").pack(anchor="w", pady=(2, 8))
        coordrow = ttk.Frame(top)
        coordrow.pack(fill="x")
        self.add_entry(coordrow, "lat", "Latitude (°)", "40.4168")
        self.add_entry(coordrow, "lon", "Longitude (°)", "-3.7038")
        self.add_entry(coordrow, "alt", "Elevation (m)", "667")
        self.add_entry(coordrow, "date", "UTC date (YYYY-MM-DD)", datetime.now(timezone.utc).strftime("%Y-%m-%d"), 17)
        self.add_entry(coordrow, "hour", "UTC time (HH:MM:SS)", datetime.now(timezone.utc).strftime("%H:%M:%S"), 15)
        controls = ttk.Frame(top)
        controls.pack(fill="x", pady=(7, 0))
        ttk.Button(controls, text="Calculate / update", command=self.calculate).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(controls, text="Celestial view (N up, E left)",
                        variable=self.view, value="sky", command=self.redraw).pack(side="left", padx=5)
        ttk.Radiobutton(controls, text="Star diagonal: horizontal mirror", variable=self.view,
                        value="mirror", command=self.redraw).pack(side="left", padx=5)
        ttk.Label(controls, text="Additional rotation (°):").pack(side="left", padx=(16, 4))
        self.rotation_box = ttk.Spinbox(controls, from_=-360, to=360, increment=5, width=6,
                                        textvariable=self.rotation, command=self.redraw)
        self.rotation_box.pack(side="left")
        self.rotation_box.bind("<Return>", lambda _e: self.redraw())
        self.rotation_box.bind("<FocusOut>", lambda _e: self.redraw())
        ttk.Button(controls, text="Save PNG", command=self.save_image).pack(side="right")
        live_controls = ttk.Frame(top)
        live_controls.pack(fill="x", pady=(8, 0))
        ttk.Button(live_controls, text="Load / refresh Visible Solar Disk", command=self.load_solar_image).pack(side="left")
        ttk.Checkbutton(live_controls, text="Show SDO image", variable=self.show_solar_image,
                        command=self.redraw).pack(side="left", padx=12)
        ttk.Checkbutton(live_controls, text="Show NOAA region labels (approx.)",
                        variable=self.show_region_labels, command=self.redraw).pack(side="left", padx=5)
        ttk.Button(live_controls, text="Load TheSkyLive sunspots", command=self.load_theskylive_image).pack(side="left", padx=5)
        ttk.Label(live_controls, textvariable=self.image_status, foreground="#455469").pack(side="left", padx=8)
        ttk.Button(live_controls, text="Source website", command=self.open_source).pack(side="right")
        photo_controls = ttk.Frame(top)
        photo_controls.pack(fill="x")
        ttk.Label(photo_controls, text="Photo source:").pack(side="left")
        ttk.Radiobutton(photo_controls, text="SpaceWeatherLive", variable=self.selected_image, value="spaceweather", command=self.select_image).pack(side="left", padx=8)
        ttk.Radiobutton(photo_controls, text="TheSkyLive", variable=self.selected_image, value="theskylive", command=self.select_image).pack(side="left")
        ttk.Button(photo_controls, text="Open TheSkyLive website", command=lambda: __import__("webbrowser").open(THESKYLIVE_PAGE)).pack(side="right")
        orient_row = ttk.Frame(top)
        orient_row.pack(fill="x", pady=(4, 2))
        ttk.Label(orient_row, text="Original photo north:").pack(side="left")
        self.photo_orientation_box = ttk.Combobox(
            orient_row, state="readonly", width=19,
            values=["Solar north up", "Celestial north up"])
        self.photo_orientation_box.pack(side="left", padx=6)
        self.photo_orientation_box.bind("<<ComboboxSelected>>", self._set_photo_orientation)
        ttk.Label(orient_row, text="Source correction (° CCW):").pack(side="left", padx=(12, 4))
        self.photo_offset_box = ttk.Entry(orient_row, width=7)
        self.photo_offset_box.pack(side="left")
        self.photo_offset_box.bind("<Return>", self._set_photo_offset)
        self.photo_offset_box.bind("<FocusOut>", self._set_photo_offset)
        ttk.Checkbutton(orient_row, text="Use photo retrieval date for P", variable=self.use_photo_epoch,
                        command=self.redraw).pack(side="left", padx=12)
        self._sync_photo_controls()
        body = ttk.Frame(self, padding=(12, 2, 12, 4))
        body.pack(fill="both", expand=True)
        self.fig = Figure(figsize=(8, 6.5), dpi=100, facecolor="#0b1323")
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=.03, right=.97, top=.97, bottom=.03)
        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        self.canvas.get_tk_widget().pack(side="left", fill="both", expand=True)
        info_panel = ttk.Frame(body, width=280, padding=(15, 20))
        info_panel.pack(side="right", fill="y")
        info_panel.pack_propagate(False)
        ttk.Label(info_panel, text="Results", font=("Arial", 15, "bold")).pack(anchor="w", pady=(0, 12))
        self.out = {}
        for key, label in [("ra", "Right ascension"), ("dec", "Declination"),
                           ("alt", "Altitude"), ("az", "Azimuth"),
                           ("P", "P angle"), ("B0", "B₀ latitude")]:
            ttk.Label(info_panel, text=label, foreground="#5b6777").pack(anchor="w", pady=(8, 0))
            variable = tk.StringVar(value="—")
            ttk.Label(info_panel, textvariable=variable, font=("Arial", 13, "bold")).pack(anchor="w")
            self.out[key] = variable
        ttk.Separator(info_panel).pack(fill="x", pady=18)
        ttk.Label(info_panel, text="SDO: latest image, not historical.\n"
                  "Photo P uses retrieval time as a proxy,\n"
                  "not a verified exposure timestamp.\n"
                  "Original north orientation is assumed,\n"
                  "not verified from image WCS.",
                  foreground="#5b6777", justify="left", wraplength=265).pack(anchor="w")
        ttk.Label(info_panel, text="Active regions (NOAA)", font=("Arial", 12, "bold")).pack(anchor="w", pady=(12, 2))
        self.regions_text = tk.Text(info_panel, width=29, height=7, wrap="word", state="disabled")
        self.regions_text.pack(anchor="w", fill="x")
        explanation = ("N/S/E/W: projected celestial directions.\n\n"
                       "Solid line: solar rotation axis.\n"
                       "Dashed line: solar equator.\n"
                       "P: position angle of solar north,\n"
                       "measured eastward from celestial north.\n\n"
                       "Reflection and rotation are adjustable;\n"
                       "actual eyepiece orientation depends\n"
                       "on the star diagonal rotation.")
        ttk.Label(info_panel, text=explanation, justify="left", wraplength=260).pack(anchor="w")
        ttk.Label(self, text="SAFETY: Observe the Sun only with a certified full-aperture solar filter in front of the objective.",
                  foreground="#9b1c1c", padding=(14, 7)).pack(side="bottom", fill="x")

    def calculate(self):
        try:
            lat = float(self.entries["lat"].get().replace(",", "."))
            lon = float(self.entries["lon"].get().replace(",", "."))
            height = float(self.entries["alt"].get().replace(",", "."))
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError("Latitude must be -90 to 90; longitude -180 to 180.")
            utc_stamp = self.entries["date"].get().strip() + " " + self.entries["hour"].get().strip()
            self.data = compute_sun(lat, lon, height, utc_stamp)
            d = self.data
            self.out["ra"].set(d["ra"].to_string(unit=u.hourangle, sep="hms", precision=1))
            self.out["dec"].set(d["dec"].to_string(unit=u.deg, sep="dms", precision=1, alwayssign=True))
            self.out["alt"].set(f'{d["alt"]:.3f}°')
            self.out["az"].set(f'{d["az"]:.3f}° (N=0°, E=90°)')
            self.out["P"].set(f'{d["P"]:+.3f}°')
            self.out["B0"].set(f'{d["B0"]:+.3f}°')
            self.redraw()
        except Exception as exc:
            messagebox.showerror("Invalid input or calculation", str(exc))

    def redraw(self):
        if self.data is None:
            return
        try:
            rotation = math.radians(float(self.rotation.get().replace(",", ".")))
        except ValueError:
            return
        ax = self.ax
        ax.clear()
        ax.set_facecolor("#0b1323")
        ax.set_aspect("equal")
        ax.set_xlim(-1.43, 1.43)
        ax.set_ylim(-1.40, 1.40)
        ax.axis("off")
        # Sky (x,y): north up and east left.
        # Horizontal reflection approximates a single-mirror diagonal.
        mirror = -1 if self.view.get() == "mirror" else 1
        c, s = math.cos(rotation), math.sin(rotation)

        def project(x, y):
            x = np.asarray(x, dtype=float) * mirror
            y = np.asarray(y, dtype=float)
            return c * x - s * y, s * x + c * y

        if self.show_solar_image.get() and self.solar_image is not None:
            # Image pixels are assumed centered on the Sun with north at image top.
            # Transform order: source correction -> P (only if solar north up)
            # -> optional star-diagonal reflection -> eyepiece field rotation.
            # Both sources have separate settings to avoid assuming equal processing.
            source = self.selected_image.get()
            try:
                offset_deg = float(self.source_offset[source].get().replace(",", "."))
                if not math.isfinite(offset_deg):
                    raise ValueError("finite angle required")
            except ValueError:
                self.image_status.set("Invalid source correction angle")
                return
            photo_p = self.data["P"]
            stamp_note = "selected ephemeris"
            if self.use_photo_epoch.get() and source in self.photo_epochs:
                try:
                    stamp = self.photo_epochs[source].strftime("%Y-%m-%d %H:%M:%S")
                    lat = float(self.entries["lat"].get().replace(",", "."))
                    lon = float(self.entries["lon"].get().replace(",", "."))
                    elev = float(self.entries["alt"].get().replace(",", "."))
                    photo_p = compute_sun(lat, lon, elev, stamp)["P"]
                    stamp_note = "retrieval-time proxy"
                except (ValueError, TypeError):
                    pass
            theta = offset_deg
            if self.source_orientation[source].get() == "solar":
                theta += photo_p
            # In chart coordinates image +x is west, +y is north.
            # A +P rotation (counterclockwise) moves image north toward (-sinP,+cosP).
            transform = Affine2D().rotate_deg(theta)
            if mirror == -1:
                transform.scale(-1.0, 1.0)
            transform.rotate(rotation)
            image_artist = ax.imshow(
                self.solar_image, extent=(-1.04, 1.04, -1.04, 1.04),
                origin="upper", transform=transform + ax.transData, zorder=2,
                interpolation="bilinear")
            # Avoid displaying rotated image corners outside the photospheric disk.
            # This mask is an approximation; individual providers may crop differently.
            image_artist.set_clip_path(Circle((0, 0), 1.04, transform=ax.transData))
            ax.add_patch(Circle((0, 0), 1.04, fill=False,
                                edgecolor="#ffffff", alpha=.45, linewidth=1, zorder=3))
            # Approximate NOAA markers are expressed in native solar coordinates.
            # They undergo the SAME transformation as the photograph. This remains
            # qualitative only: there is no verified WCS or synchronized image epoch.
            if source == "spaceweather" and self.show_region_labels.get() and self.active_regions:
                b0 = math.radians(self.data["B0"])
                for region in self.active_regions:
                    phi = math.radians(region["latitude"])
                    lam = math.radians(region["longitude"])
                    mu = (math.sin(phi)*math.sin(b0)
                          + math.cos(phi)*math.cos(b0)*math.cos(lam))
                    if mu <= 0:
                        continue
                    rx = .94*math.cos(phi)*math.sin(lam)
                    ry = .94*(math.sin(phi)*math.cos(b0)
                              - math.cos(phi)*math.cos(lam)*math.sin(b0))
                    if self.source_orientation[source].get() != "solar":
                        # Convert nominal solar coordinates to celestial coordinates
                        # for source images declared celestial-north-up.
                        pp = math.radians(photo_p)
                        rx, ry = (math.cos(pp)*rx - math.sin(pp)*ry,
                                  math.sin(pp)*rx + math.cos(pp)*ry)
                    point = transform.transform((rx, ry))
                    ax.scatter([point[0]], [point[1]], s=110, facecolors="none",
                               edgecolors="#00e7f7", linewidths=1.7, zorder=9)
                    ax.annotate(region["number"], point, xytext=(6, 7),
                                textcoords="offset points", color="white", zorder=10,
                                fontsize=9,
                                bbox={"facecolor":"#142a42", "alpha":.8, "edgecolor":"none"})
            # Show the predicted solar-north position on the transformed image.
            north_x, north_y = project(-math.sin(math.radians(photo_p)),
                                        math.cos(math.radians(photo_p)))
            ax.plot([0, north_x*.99], [0, north_y*.99], color="#5fe1f2",
                    linewidth=1.5, linestyle="--", alpha=.8, zorder=8)
            ax.scatter([north_x*.99], [north_y*.99], marker="^", color="#5fe1f2", s=60, zorder=9)
            ax.annotate("Solar N", (north_x*.99, north_y*.99), xytext=(5, 5),
                        textcoords="offset points", color="#5fe1f2", fontsize=10, zorder=10)
            # Cardinal labels describe the selected sky/diagonal view, not source pixels.
            for label, dx, dy in [("N", 0, 1), ("S", 0, -1),
                                  ("E", -1, 0), ("W", 1, 0)]:
                xx, yy = project(dx*1.24, dy*1.24)
                ax.text(float(xx), float(yy), label, fontsize=14,
                        fontweight="bold", color="white", ha="center", va="center", zorder=10)
            ax.text(0, -1.34,
                    f"P={photo_p:+.2f}° ({stamp_note}); source {self.source_orientation[source].get()} north-up ASSUMED",
                    ha="center", color="#ffd887", fontsize=9, zorder=12)
            ax.set_title(f"{self.image_source} / NASA SDO HMI — image rotated to sky / diagonal view",
                         color="white", fontsize=12, pad=14)
            self.canvas.draw_idle()
            return
        disc = Circle((0, 0), 1, facecolor="#f7c566",
                      edgecolor="#ffe8a4", linewidth=2, zorder=2)
        ax.add_patch(disc)
        ax.add_patch(Circle((0, 0), 1.017, fill=False, edgecolor="#ffebbd", alpha=.25, linewidth=7))
        p = math.radians(self.data["P"])
        # Solar north direction in sky axes: x West(+), y North(+)
        north_solar = np.array([-math.sin(p), math.cos(p)])
        across_solar = np.array([math.cos(p), math.sin(p)])
        v = np.linspace(-1, 1, 300)
        x, y = project(v * north_solar[0], v * north_solar[1])
        ax.plot(x, y, color="#1e4777", linewidth=2.7, zorder=4)
        # Solar equator: great circle projected as ellipse, minor semi-axis |sin B0|.
        t = np.linspace(0, 2 * math.pi, 500)
        b0 = math.radians(self.data["B0"])
        eq = np.outer(np.cos(t), across_solar) + np.outer(np.sin(t) * math.sin(b0), north_solar)
        ex, ey = project(eq[:, 0], eq[:, 1])
        ax.plot(ex, ey, color="#863000", linestyle="--", linewidth=2.2, zorder=5)
        # Solar poles (the far pole lies on the unseen hemisphere).
        px, py = project(north_solar[0] * math.cos(b0), north_solar[1] * math.cos(b0))
        ax.scatter([px], [py], s=55, c="#123f79", edgecolors="white", zorder=7)
        ax.annotate("Solar N pole", (px, py), xytext=(11, 11), textcoords="offset points",
                    color="#062c5c", fontsize=11, fontweight="bold", zorder=9)
        spx, spy = project(-north_solar[0] * math.cos(b0), -north_solar[1] * math.cos(b0))
        ax.scatter([spx], [spy], s=40, c="#7e2b14", edgecolors="white", zorder=7)
        # Projected cardinal directions in the celestial coordinate system.
        cardinal = [("N", 0, 1), ("S", 0, -1), ("E", -1, 0), ("W", 1, 0)]
        for label, dx, dy in cardinal:
            xx, yy = project(dx * 1.24, dy * 1.24)
            ax.text(float(xx), float(yy), label, fontsize=16, fontweight="bold",
                    color="white", ha="center", va="center", zorder=10)
        ax.set_title(f'Sun · {self.entries["date"].get()} {self.entries["hour"].get()} UTC',
                     color="white", fontsize=14, pad=14)
        self.canvas.draw_idle()

    def open_source(self):
        import webbrowser
        webbrowser.open(SOURCE_PAGE)

    @staticmethod
    def parse_regions(html: bytes):
        """Extract NOAA number and heliographic latitude / central-meridian distance.

        Site markup can change; an empty result is handled explicitly.
        """
        soup = BeautifulSoup(html, "html.parser")
        regions = {}
        for h in soup.find_all(["h2", "h3", "h4"]):
            heading = h.get_text(" ", strip=True)
            match = re.search(r"(?:Region|Región)\s*(\d{4,5})\b", heading, re.I)
            if not match:
                continue
            number = match.group(1)
            pieces = []
            for following in h.next_elements:
                if getattr(following, "name", None) in ("h2", "h3", "h4") and following is not h:
                    break
                if getattr(following, "name", None) == "table":
                    pieces.append(following.get_text(" ", strip=True))
            body = " ".join(pieces)
            loc = re.search(r"\b([NS])\s*(\d{1,2})\s*([EW])\s*(\d{1,3})\b", body, re.I)
            if not loc:
                continue
            lat = int(loc.group(2)) * (1 if loc.group(1).upper() == "N" else -1)
            lon = int(loc.group(4)) * (1 if loc.group(3).upper() == "W" else -1)
            if abs(lat) <= 90 and abs(lon) <= 90:
                regions[number] = {"number": number, "latitude": lat,
                                   "longitude": lon, "location": loc.group(0).replace(" ", "")}
        return list(regions.values())

    def update_region_list(self):
        self.regions_text.configure(state="normal")
        self.regions_text.delete("1.0", "end")
        if self.active_regions:
            for r in self.active_regions:
                self.regions_text.insert("end", f'AR {r["number"]}  {r["location"]}\n')
        else:
            self.regions_text.insert("end", "No NOAA regions parsed from the current page.\n"
                                     "Open Source website to verify labels.")
        self.regions_text.configure(state="disabled")

    def load_solar_image(self):
        if self.image_busy:
            return
        self.image_busy = True
        self.image_status.set("Downloading latest SDO image…")
        # Fetch off the Tk event thread to avoid freezing the GUI.
        def worker():
            try:
                request = urllib.request.Request(
                    VISIBLE_SOLAR_DISK_URL,
                    headers={"User-Agent": "SolarOrientationED80/1.1 (educational desktop application)",
                             "Cache-Control": "no-cache"})
                with urllib.request.urlopen(request, timeout=15) as reply:
                    payload = reply.read(8_000_001)
                if len(payload) > 8_000_000:
                    raise ValueError("Image exceeds 8 MB limit")
                image = Image.open(BytesIO(payload))
                image.verify()
                image = Image.open(BytesIO(payload)).convert("RGB")
                # Fetch independently published NOAA descriptions as well.
                regions = []
                regions_error = None
                try:
                    page_req = urllib.request.Request(SOURCE_PAGE, headers={
                        "User-Agent": "Mozilla/5.0 (compatible; SolarOrientationED80/1.2)",
                        "Cache-Control": "no-cache"})
                    with urllib.request.urlopen(page_req, timeout=15) as reply:
                        html = reply.read(2_000_001)
                    if len(html) > 2_000_000:
                        raise ValueError("Region page exceeds 2 MB")
                    regions = self.parse_regions(html)
                    if not regions:
                        regions_error = "No NOAA regions found on source page"
                except Exception as exc:
                    regions_error = str(exc)
                self.after(0, lambda: self._finish_image(image, regions, regions_error, None))
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda: self._finish_image(None, [], None, error))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_image(self, image, regions, regions_error, error):
        self.image_busy = False
        if error is not None:
            self.image_status.set("Download failed")
            messagebox.showwarning("Visible Solar Disk", f"Could not load image:\n{error}")
            return
        self.spaceweather_image = np.asarray(image)
        self.selected_image.set("spaceweather")
        self.select_image()
        self.active_regions = regions
        self.update_region_list()
        self.show_solar_image.set(True)
        self.image_loaded_at = datetime.now(timezone.utc)
        self.photo_epochs["spaceweather"] = self.image_loaded_at
        self.image_status.set(f"SDO loaded · {len(regions)} NOAA regions" +
                              (" · labels unavailable" if regions_error else ""))
        self.redraw()

    def select_image(self):
        """Show the cached image belonging to the selected website, if loaded."""
        if self.selected_image.get() == "theskylive":
            self.solar_image = self.theskylive_image
            self.image_source = "TheSkyLive"
        else:
            self.solar_image = self.spaceweather_image
            self.image_source = "SpaceWeatherLive"
        if self.solar_image is None:
            self.image_status.set(f"{self.image_source}: not loaded")
        self._sync_photo_controls()
        self.redraw()

    @staticmethod
    def theskylive_photo_url(html):
        """Find the image INSIDE 'Today's Sunspots Activity', not unrelated charts."""
        soup = BeautifulSoup(html, "html.parser")
        heading = next((h for h in soup.find_all(["h1", "h2", "h3", "h4"])
                        if "today" in h.get_text(" ", strip=True).casefold()
                        and "sunspot" in h.get_text(" ", strip=True).casefold()), None)
        if heading is None:
            raise ValueError("Today's Sunspots Activity section was not found")
        candidates = []
        for el in heading.next_elements:
            if getattr(el, 'name', None) in ('h1', 'h2', 'h3', 'h4'):
                break
            if getattr(el, 'name', None) in ('img', 'source'):
                for attr in ('data-src', 'data-lazy-src', 'data-original', 'src', 'srcset', 'data-srcset'):
                    val = el.get(attr)
                    if not val:
                        continue
                    # For srcset use the first candidate; no speculative URL guesses.
                    path = str(val).split(',')[0].strip().split(' ')[0]
                    url = urllib.parse.urljoin(THESKYLIVE_PAGE, path)
                    if urllib.parse.urlparse(url).scheme in ('https', 'http'):
                        candidates.append(url)
        if not candidates:
            raise ValueError("No solar photograph URL found in the sunspots section")
        return candidates[0]

    def load_theskylive_image(self):
        """Load actual photo identified in TheSkyLive page; never substitute another source."""
        if self.image_busy:
            return
        self.image_busy = True
        self.image_status.set("Loading TheSkyLive photograph…")

        def worker():
            try:
                headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Safari/537.36",
                           "Accept": "text/html,image/avif,image/webp,image/*;q=0.9,*/*;q=0.8"}
                request = urllib.request.Request(THESKYLIVE_PAGE, headers=headers)
                with urllib.request.urlopen(request, timeout=20) as reply:
                    html = reply.read(4_000_001)
                if len(html) > 4_000_000:
                    raise ValueError("HTML exceeds the 4 MB safety limit")
                photo_url = self.theskylive_photo_url(html)
                img_headers = {**headers, "Referer": THESKYLIVE_PAGE}
                request = urllib.request.Request(photo_url, headers=img_headers)
                with urllib.request.urlopen(request, timeout=20) as reply:
                    payload = reply.read(12_000_001)
                if len(payload) > 12_000_000:
                    raise ValueError("Photograph exceeds the 12 MB safety limit")
                photo = Image.open(BytesIO(payload))
                photo.verify()
                photo = Image.open(BytesIO(payload)).convert("RGB")
                self.after(0, lambda: self.finish_theskylive(photo, None))
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda: self.finish_theskylive(None, error))

        threading.Thread(target=worker, daemon=True).start()

    def finish_theskylive(self, photo, error):
        self.image_busy = False
        if error:
            self.image_status.set("TheSkyLive: photo unavailable")
            messagebox.showwarning("TheSkyLive Sunspots",
                "Could not retrieve the photo from the Today's Sunspots Activity section.\n"
                "The provider may block automated requests (HTTP 403) or may have changed its HTML.\n\n"
                f"Details: {error}\n\nUse 'Open TheSkyLive website' to view it in your browser.")
            return
        self.theskylive_image = np.asarray(photo)
        self.photo_epochs["theskylive"] = datetime.now(timezone.utc)
        self.selected_image.set("theskylive")
        self.show_solar_image.set(True)
        self.select_image()
        self.image_status.set("TheSkyLive photograph loaded")
        self.redraw()

    def _sync_photo_controls(self):
        source = self.selected_image.get()
        orient = self.source_orientation[source].get()
        self.photo_orientation_box.set("Solar north up" if orient == "solar" else "Celestial north up")
        self.photo_offset_box.delete(0, "end")
        self.photo_offset_box.insert(0, self.source_offset[source].get())

    def _set_photo_orientation(self, _event=None):
        source = self.selected_image.get()
        self.source_orientation[source].set(
            "solar" if self.photo_orientation_box.get() == "Solar north up" else "celestial")
        self.redraw()

    def _set_photo_offset(self, _event=None):
        try:
            value = float(self.photo_offset_box.get().replace(",", "."))
            if not math.isfinite(value):
                raise ValueError("Angle must be finite")
        except ValueError:
            messagebox.showerror("Invalid angle", "Source correction must be a number in degrees.")
            return
        self.source_offset[self.selected_image.get()].set(str(value))
        self.redraw()

    def save_image(self):
        if not self.data:
            return
        path = filedialog.asksaveasfilename(title="Save chart", defaultextension=".png",
                                            filetypes=[("PNG image", "*.png")],
                                            initialfile="solar_orientation.png")
        if path:
            try:
                self.fig.savefig(path, dpi=200, facecolor=self.fig.get_facecolor(), bbox_inches="tight")
            except Exception as exc:
                messagebox.showerror("Save error", str(exc))


if __name__ == "__main__":
    SolarOrientationApp().mainloop()
