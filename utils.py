def polygon_orientation(coords):
    # coords = [(x1,y1), (x2,y2), ...]
    area = 0
    for i in range(len(coords)):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % len(coords)]
        area += (x2 - x1) * (y2 + y1)
    return "CW" if area > 0 else "CCW"

def line_normals(p1, p2, orientation="CCW"):
    dx, dy = p2[0]-p1[0], p2[1]-p1[1]
    if orientation == "CCW":  # inside = left normal
        inside = (-dy, dx)
        outside = (dy, -dx)
    else:  # CW polygon
        inside = (dy, -dx)
        outside = (-dy, dx)
    return inside, outside

def line_direction(angle) -> str:
    # Normalize angle between -180 and 180
    if -90 <= angle <= 90:
        return "left → right"
    else:
        return "right → left"