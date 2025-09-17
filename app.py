import os
import math

from flask import Flask, request, jsonify

from dxf import SurveyDXFManager
from models.plan import PlanProps
from utils import polygon_orientation, line_normals, line_direction, html_to_mtext

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret"


@app.get("/")
def home():
    return "<h1>Hello, Flask 👋</h1><p>You're up and running!</p>"

@app.route("/cadastral/plan", methods=["POST"])
def generate_cadastral_plan():
    data = request.get_json()

    plan = PlanProps(**data)
    extent = plan.get_extent()
    beacon_size = extent * 0.02

    drawer = SurveyDXFManager(plan_name=plan.name, scale=plan.get_drawing_scale())
    drawer.setup_beacon_style(type_=plan.beacon_type, size=beacon_size or 1.0)
    drawer.setup_font(plan.font)

    label_height = extent * 0.015 if extent > 0 else 1.0

    # Draw beacon and labels
    for coord in plan.coordinates:
        drawer.add_beacon(coord.easting, coord.northing, 0, beacon_size * 0.5, coord.id)

    # create a dictionary of coordinates for easy lookup
    coord_dict = {coord.id: coord for coord in plan.coordinates}
    parcel_dict = {}

    # Draw parcels
    for parcel in plan.parcels:
        parcel_points = []
        for point_id in parcel.ids:
            if point_id in coord_dict:
                coord = coord_dict[point_id]
                parcel_points.append((coord.easting, coord.northing))
        if parcel_points:
            drawer.add_parcel(parcel.name, parcel_points)

            # add bearing and distance text
            orientation = polygon_orientation(parcel_points)
            for leg in parcel.legs:
                # compute rotational angle for text
                angle_rad = math.atan2(leg.to.northing - leg.from_.northing, leg.to.easting - leg.from_.easting)
                angle_deg = math.degrees(angle_rad)

                first_x = leg.from_.easting + (0.2 * (leg.to.easting - leg.from_.easting))
                first_y = leg.from_.northing + (0.2 * (leg.to.northing - leg.from_.northing))
                last_x = leg.from_.easting + (0.8 * (leg.to.easting - leg.from_.easting))
                last_y = leg.from_.northing + (0.8 * (leg.to.northing - leg.from_.northing))
                mid_x = (leg.from_.easting + leg.to.easting) / 2
                mid_y = (leg.from_.northing + leg.to.northing) / 2

                # Offset text above/below the line
                normals = line_normals((leg.from_.easting, leg.from_.northing), (leg.to.easting, leg.to.northing), orientation)
                offset_distance = extent * 0.02
                offset_inside_x = (normals[0][0] / math.hypot(*normals[0])) * offset_distance
                offset_inside_y = (normals[0][1] / math.hypot(*normals[0])) * offset_distance
                offset_outside_x = (normals[1][0] / math.hypot(*normals[1])) * offset_distance
                offset_outside_y = (normals[1][1] / math.hypot(*normals[1])) * offset_distance
                first_x += offset_outside_x
                first_y += offset_outside_y
                last_x += offset_outside_x
                last_y += offset_outside_y
                mid_x += offset_inside_x
                mid_y += offset_inside_y

                # add texts
                text_angle = angle_deg
                if text_angle > 90 or text_angle < -90:
                    text_angle += 180

                drawer.add_text(f"{leg.distance:.2f} m", mid_x, mid_y, angle=text_angle, height=label_height)
                ld = line_direction(angle_deg)
                if ld == "left → right":
                    drawer.add_text(f"{leg.bearing.degrees}°", first_x, first_y, angle=text_angle, height=label_height)
                    drawer.add_text(f"{leg.bearing.minutes}'", last_x, last_y, angle=text_angle, height=label_height)
                else:
                    drawer.add_text(f"{leg.bearing.degrees}°", last_x, last_y, angle=text_angle, height=label_height)
                    drawer.add_text(f"{leg.bearing.minutes}'", first_x, first_y, angle=text_angle, height=label_height)
            parcel_dict[parcel.name] = parcel_points

    # Compute extent sizes
    min_x, min_y, max_x, max_y = plan.get_bounding_box()
    width = max_x - min_x
    height = max_y - min_y

    # Draw frame
    margin_x = max(width, height) * 0.35
    margin_y = max(height, width) * 0.7
    frame_left = min_x - margin_x
    frame_bottom = min_y - margin_y
    frame_right = max_x + margin_x
    frame_top = max_y + margin_y
    drawer.draw_frame(frame_left, frame_bottom, frame_right,frame_top)

    # offset frame
    offset_x = max(width, height) * 0.38
    offset_y = max(height, width) * 0.73
    drawer.draw_frame(min_x - offset_x, min_y - offset_y, max_x + offset_x, max_y + offset_y)

    # add title block
    # Calculate center position for title
    frame_width = frame_right - frame_left
    frame_center_x = frame_left + (frame_width / 2)

    # Title positioning
    title_y = frame_top - (margin_y * 0.2)  # 5 units below the top of frame
    title_height = plan.font_size  # Height of title text
    title_width = frame_width * 0.6  # Use 60% of frame width for title box

    drawer.add_title(html_to_mtext(plan.build_title()), frame_center_x, title_y, title_width, title_height)

    # drawer.save_dxf()
    # drawer.dxf_to_dwg()
    drawer.save()
    return jsonify({"message": "Cadastral plan generated", "filename": plan.name}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
