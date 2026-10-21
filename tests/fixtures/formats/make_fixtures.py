# pylint: disable=invalid-name,line-too-long  # a one-off generator of literal file formats
"""Write the three small conversion fixtures (run once; the outputs are committed).

Hand-built so no PDF or Word library is needed: a minimal PDF with two text lines,
and a minimal .docx with a heading and a paragraph.
"""

import zipfile
from pathlib import Path

HERE = Path(__file__).parent
TITLE, BODY = "Delta Devices", "Delta Devices designs wearable glucose monitors."

(HERE / "profile.html").write_text(
    f"<html><body><h1>{TITLE}</h1><h2>Overview</h2><p>{BODY}</p></body></html>\n",
    encoding="utf-8",
)

DOCX_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {W}><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>"""
DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {W}><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{TITLE}</w:t></w:r></w:p>
<w:p><w:r><w:t>{BODY}</w:t></w:r></w:p>
</w:body></w:document>"""
with zipfile.ZipFile(HERE / "profile.docx", "w") as z:
    z.writestr("[Content_Types].xml", DOCX_TYPES)
    z.writestr("_rels/.rels", DOCX_RELS)
    z.writestr("word/_rels/document.xml.rels", DOC_RELS)
    z.writestr("word/styles.xml", STYLES)
    z.writestr("word/document.xml", DOCUMENT)

stream = (
    f"BT /F1 18 Tf 72 720 Td ({TITLE}) Tj 0 -30 Td /F1 12 Tf ({BODY}) Tj ET".encode()
)
objects = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
    b"/Resources << /Font << /F1 5 0 R >> >> >>",
    b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
]
pdf, offsets = bytearray(b"%PDF-1.4\n"), []
for n, body in enumerate(objects, 1):
    offsets.append(len(pdf))
    pdf += b"%d 0 obj\n" % n + body + b"\nendobj\n"
xref = len(pdf)
pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
pdf += b"".join(b"%010d 00000 n \n" % o for o in offsets)
pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
    len(objects) + 1,
    xref,
)
(HERE / "profile.pdf").write_bytes(bytes(pdf))
