"""Small geometry helpers: local metric frame and projection of points onto a polyline."""
import math
import numpy as np

R_EARTH = 111320.0  # metres per degree of latitude


class Frame:
    """Equirectangular frame centred on (lat0, lon0). Fine for city-scale distances."""

    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = float(lat0), float(lon0)
        self.kx = R_EARTH * math.cos(math.radians(self.lat0))

    def to_xy(self, lat, lon):
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * R_EARTH

    def to_ll(self, x, y):
        x, y = np.asarray(x, float), np.asarray(y, float)
        return self.lat0 + y / R_EARTH, self.lon0 + x / self.kx


def cumlen(xy):
    d = np.sqrt((np.diff(xy, axis=0) ** 2).sum(1))
    return np.r_[0.0, np.cumsum(d)]


def project(pts, shape_xy, cum, chunk=4000):
    """Project points (N,2) onto a polyline. Returns (s, d): distance along the line
    and perpendicular distance to it, both in metres."""
    pts = np.atleast_2d(pts)
    a, b = shape_xy[:-1], shape_xy[1:]
    ab = b - a
    L2 = np.maximum((ab ** 2).sum(1), 1e-9)
    s_out = np.empty(len(pts))
    d_out = np.empty(len(pts))
    for i in range(0, len(pts), chunk):
        p = pts[i:i + chunk]
        ap = p[:, None, :] - a[None, :, :]
        t = np.clip((ap * ab[None]).sum(2) / L2[None], 0, 1)
        proj = a[None] + t[..., None] * ab[None]
        dist = np.sqrt(((p[:, None, :] - proj) ** 2).sum(2))
        j = dist.argmin(1)
        r = np.arange(len(p))
        s_out[i:i + chunk] = cum[j] + t[r, j] * np.sqrt(L2[j])
        d_out[i:i + chunk] = dist[r, j]
    return s_out, d_out


def point_at(shape_xy, cum, s):
    s = np.clip(np.asarray(s, float), 0, cum[-1])
    return np.interp(s, cum, shape_xy[:, 0]), np.interp(s, cum, shape_xy[:, 1])


def bearing_bucket(x0, y0, x1, y1):
    ang = (math.degrees(math.atan2(x1 - x0, y1 - y0)) + 360) % 360
    return int(((ang + 22.5) % 360) // 45)
