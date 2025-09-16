from bs4 import BeautifulSoup
from ezdxf.tools.text import MTextEditor

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

def html_to_mtext(html_text: str):
    if not html_text:
        return ""

    html_text = html_text.replace('\n', '')
    print(html_text)
    soup = BeautifulSoup(html_text, "html.parser")
    editor = MTextEditor()

    def parse_tag(tag):
        for child in tag.children:
            if isinstance(child, str):  # Plain text
                editor.append(child.strip())
            else:
                # Apply formatting recursively
                if child.name == "b" or child.name == "strong":
                    editor.append("\\B")
                    parse_tag(child)
                    editor.append("\\b")
                elif child.name == "i":
                    editor.append("\\I")
                    parse_tag(child)
                    editor.append("\\i")
                elif child.name == "u":
                    editor.append(MTextEditor.UNDERLINE_START)
                    parse_tag(child)
                    editor.append(MTextEditor.UNDERLINE_STOP)
                elif child.name == "br":
                    editor.append(MTextEditor.NEW_LINE)
                elif child.name == "p":
                    prev = child.previous_sibling
                    while prev and str(prev).strip() == "":
                        prev = prev.previous_sibling
                    if prev is not None:
                        editor.append(MTextEditor.NEW_LINE)
                    parse_tag(child)
                    # editor.append(MTextEditor.NEW_LINE)
                else:
                    parse_tag(child)

    parse_tag(soup)
    result = str(editor)
    result = result.replace('\n', '')
    return result


if __name__ == '__main__':
    print(repr(html_to_mtext()))
