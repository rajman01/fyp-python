from flask import Flask, request, jsonify

from dxf import SurveyDXFManager
from models.plan import PlanProps

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
    beacon_size = extent * 0.05

    drawer = SurveyDXFManager(plan_name=plan.name, scale=plan.scale)
    drawer.setup_beacon_style(type_=plan.beacon_type, size=beacon_size or 1.0)
    drawer.setup_font(plan.font)

    # Draw beacon and labels
    for coord in plan.coordinates:
        drawer.add_beacon(coord.easting, coord.northing, 0, beacon_size * 0.5, coord.id)

    # create a dictionary of coordinates for easy lookup
    coord_dict = {coord.id: coord for coord in plan.coordinates}

    # Draw parcels
    for parcel in plan.parcels:
        parcel_points = []
        for point_id in parcel.ids:
            if point_id in coord_dict:
                coord = coord_dict[point_id]
                parcel_points.append((coord.easting, coord.northing, coord.elevation))
        if parcel_points:
            drawer.add_parcel(parcel.name, parcel_points)

    # Compute extent sizes
    min_x, min_y, max_x, max_y = plan.get_bounding_box()
    width = max_x - min_x
    height = max_y - min_y

    # Draw frame
    margin_x = max(width, height) * 0.4
    margin_y = max(height, width) * 0.75
    drawer.draw_frame(min_x - margin_x, min_y - margin_y, max_x + margin_x, max_y + margin_y)

    # offset frame
    offset_x = max(width, height) * 0.43
    offset_y = max(height, width) * 0.78
    drawer.draw_frame(min_x - offset_x, min_y - offset_y, max_x + offset_x, max_y + offset_y)

    # add bearing and distance text

    drawer.dxf_to_pdf()
    return jsonify({"message": "Cadastral plan generated", "filename": plan.name}), 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8080, debug=True)
