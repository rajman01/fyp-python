import os
from dotenv import load_dotenv

from topographic import TopographicPlan

load_dotenv()  # reads .env into environment

from cadastral import CadastralPlan

from flask import Flask, request, jsonify

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret"


@app.get("/")
def home():
    return "<h1>Hello, Flask 👋</h1><p>You're up and running!</p>"

@app.route("/cadastral/plan", methods=["POST"])
def generate_cadastral_plan():
    data = request.get_json()

    plan = CadastralPlan(**data)
    plan.draw()

    url = plan.save()
    return jsonify({"message": "Cadastral plan generated", "filename": plan.name, "url": url}), 200

@app.route("/topographic/plan", methods=["POST"])
def generate_topographic_plan():
    data = request.get_json()

    plan = TopographicPlan(**data)
    plan.draw()

    url = plan.save()
    return jsonify({"message": "Topographic plan generated", "filename": plan.name, "url": url}), 200

# @app.route("/route/plan", methods=["POST"])
# def generate_route_plan():
#     data = request.get_json()
#     plan = PlanProps(**data)
#     return jsonify({"message": "Cadastral plan generated", "filename": plan.name, "url": "url"}), 200

# def read_json_file(file_path: str):
#     """
#     Reads data from a JSON file.
#
#     Args:
#         file_path (str): Path to the JSON file.
#
#     Returns:
#         dict | list: Parsed JSON data.
#     """
#     try:
#         with open(file_path, "r", encoding="utf-8") as f:
#             data = json.load(f)
#         return data
#     except FileNotFoundError:
#         print(f"Error: File '{file_path}' not found.")
#         return None
#     except json.JSONDecodeError as e:
#         print(f"Error: Failed to decode JSON - {e}")
#         return None

# @app.route("/topographic/plan", methods=["POST"])
# def generate_topographic_plan():
#     data = request.get_json()
#
#     plan = PlanProps(**data)
#
#     drawer = SurveyDXFManager(plan_name=plan.name, scale=plan.get_drawing_scale())
#     drawer.setup_font(plan.font)
#     drawer.setup_topo_point_style()
#
#     data = read_json_file("point2.json")
#     plan.coordinates = [CoordinateProps(**c) for c in data]
#
#     # draw spot heights
#     # for coord in plan.coordinates:
#     #     drawer.add_topo_point(coord.easting, coord.northing, coord.elevation, f"{coord.elevation:.3f}", plan.top_setting.point_label_scale)
#
#     # Generate a surface (TIN interpolation).
#     x = np.array([coord.easting for coord in plan.coordinates])
#     y = np.array([coord.northing for coord in plan.coordinates])
#     z = np.array([coord.elevation for coord in plan.coordinates])
#
#     # Create triangulation
#     triangulation = Triangulation(x, y)
#
#     # Generate contour levels
#     z_min, z_max = z.min(), z.max()
#     levels = np.linspace(z_min, z_max, 100)
#
#     # Create matplotlib contours (using memory buffer to avoid display)
#     contours = plt.tricontour(triangulation, z, levels=levels)

#     # Define major contour interval (every 5th contour)
#     major_interval = max(1, len(levels) // 5)
#
#     # Extract and draw contour lines
#     contour_data = []
#
#     # Access contour segments using allsegs attribute (more reliable)
#     if hasattr(contours, 'allsegs') and len(contours.allsegs) > 0:
#         for level_idx, level_segments in enumerate(contours.allsegs):
#             elevation = levels[level_idx]
#         is_major = (level_idx % major_interval == 0)
#             layer_name = 'CONTOURS_MAJOR' if is_major else 'CONTOURS_MINOR'
#
#             # Process each contour segment at this elevation
#             for segment in level_segments:
#                 if len(segment) < 2:
#                     continue
#
#                 # Convert to list of tuples for ezdxf
#                 points = [(float(x), float(y), float(elevation)) for x, y in segment]
#
#                 # Add polyline to DXF
#                 polyline = drawer.msp.add_polyline3d(
#                     points,
#                     dxfattribs={'layer': layer_name}
#                 )
#
#                 # Store contour data
#                 contour_data.append({
#                     'elevation': elevation,
#                     'coordinates': segment,
#                     'is_major': is_major,
#                     'polyline': polyline
#                 })
#
#                 # Add elevation labels for major contours
#                 if is_major and len(points) > 0:
#                     # Place label at midpoint of contour
#                     mid_idx = len(points) // 2
#                     label_x, label_y, _ = points[mid_idx]
#
#                     drawer.msp.add_text(
#                         f"{elevation:.1f}",
#                         dxfattribs={
#                             'layer': 'CONTOUR_LABELS',
#                             'height': 2.5,
#                             'style': 'Standard'
#                         }
#                     ).set_placement((label_x, label_y), align=TextEntityAlignment.MIDDLE_CENTER)
#     else:
#         print("Warning: No contour segments found. Check your input data.")
#
#     # # Create triangulation
#     # triangulation = Triangulation(x, y)
#     #
#     # # Draw triangle edges
#     # for triangle in triangulation.triangles:
#     #     # Get the three vertices of each triangle
#     #     p1 = (x[triangle[0]], y[triangle[0]])
#     #     p2 = (x[triangle[1]], y[triangle[1]])
#     #     p3 = (x[triangle[2]], y[triangle[2]])
#     #
#     #     # Create closed polyline for triangle
#     #     triangle_points = [p1, p2, p3, p1]  # Close the triangle
#     #
#     #     drawer.msp.add_lwpolyline(
#     #         triangle_points,
#     #         dxfattribs={'layer': "TIN_TRIANGLES"}
#     #     )
#
#     # # Find range
#     # z_min, z_max = z.min(), z.max()
#     #
#     # # Choose interval (e.g., 1 meter)
#     # interval = 0.1
#     #
#     # # Define levels
#     # levels = np.arange(np.floor(z_min), np.ceil(z_max) + interval, interval)
#     #
#     # contours = plt.tricontour(triang, z, levels=levels)
#     #
#     # # ✅ Each contour level has multiple paths
#     # for level, path_collection in zip(contours.levels, contours.get_paths()):
#     #     for polygon in path_collection.to_polygons():
#     #         points = [(pt[0], pt[1], float(level)) for pt in polygon]
#     #         if len(points) > 1:
#     #             # Create 3D polyline
#     #             drawer.msp.add_polyline3d(points, dxfattribs={"layer": "CONTOURS"})
#
#     drawer.save_dxf()
#     # url = drawer.save()
#     return jsonify({"message": "Topographic plan generated", "filename": plan.name, "url": "url"}), 200



@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Resource not found"}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Something went wrong on our side"}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    # You can log the exception here
    app.logger.error(f"Unhandled Exception: {e}", exc_info=True)

    # Return JSON response instead of crashing
    return jsonify({"error": "An unexpected error occurred"}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
