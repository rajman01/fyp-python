from bs4 import BeautifulSoup, Tag
from ezdxf.tools.text import MTextEditor


def html_to_mtext(html_text: str):
    if not html_text:
        return ""

    soup = BeautifulSoup(html_text, "html.parser")
    editor = MTextEditor()

    def parse_tag(tag):
        for child in tag.children:
            if isinstance(child, str):  # Plain text
                editor.append(child)
            else:
                # Apply formatting recursively
                if child.name == "b" or child.name == "strong":
                    editor.font("Arial", bold=True)
                    parse_tag(child)
                # elif child.name == "i":
                #     editor.font(child.get_text(), bold=False, italic=True)
                elif child.name == "u":
                    editor.append(MTextEditor.UNDERLINE_START)
                    parse_tag(child)
                    editor.append(MTextEditor.UNDERLINE_STOP)
                elif child.name == "br":
                    editor.append(MTextEditor.NEW_LINE)
                elif child.name == "p":
                    editor.append(MTextEditor.NEW_LINE)
                    parse_tag(child)
                    editor.append(MTextEditor.NEW_LINE)
                else:
                    parse_tag(child)

    parse_tag(soup)
    return str(editor)


if __name__ == "__main__":
    print(html_to_mtext("<p>PLAN SHEWING PROPERTY</p><p>SAID TO BELONG TO</p><p><strong>MR. BABAWALE OLATUNBO OKUNOLA</strong></p><p><strong>&amp;</strong></p><p><strong>MRS. AZEEZAT OLUSEYI OKUNOLA</strong></p><p>Hello Sir</p>"))