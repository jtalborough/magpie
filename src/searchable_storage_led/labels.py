from __future__ import annotations

import io
import socket
import textwrap
from dataclasses import asdict, dataclass, field
from urllib.parse import quote

import qrcode
from qrcode.image.svg import SvgPathImage

from .config import ZebraConfig
from .locations import Location
from .store import SearchResult


@dataclass(frozen=True)
class LabelPayload:
    location: Location
    path: list[Location]
    contents: list[SearchResult]
    base_url: str
    workspace_id: str = ""

    @property
    def url(self) -> str:
        if self.workspace_id:
            return (
                f"{self.base_url.rstrip('/')}/scan/{quote(self.workspace_id, safe='')}"
                f"/l/{quote(self.location.id, safe='')}"
            )
        return f"{self.base_url.rstrip('/')}/l/{self.location.id}"


@dataclass(frozen=True)
class ItemLabelPayload:
    item: SearchResult
    rows: list[SearchResult]
    base_url: str
    workspace_id: str = ""
    location_paths: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        if self.workspace_id:
            return (
                f"{self.base_url.rstrip('/')}/scan/{quote(self.workspace_id, safe='')}"
                f"/i/{self.item.item_id}"
            )
        return f"{self.base_url.rstrip('/')}/i/{self.item.item_id}"


@dataclass(frozen=True)
class InventoryReportRow:
    item_id: int
    name: str
    quantity: float
    unit: str
    location_id: str
    location_path: str
    category: str = ""
    location_active: bool = True


def zpl_escape(value: str) -> str:
    return (
        value.replace("^", " ")
        .replace("~", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def wrap_lines(value: str, *, width: int, limit: int) -> list[str]:
    clean = zpl_escape(value)
    lines = textwrap.wrap(clean, width=width, break_long_words=False, break_on_hyphens=False)
    return lines[:limit] if lines else []


def render_qr_svg(value: str) -> str:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    return svg.replace("<?xml version='1.0' encoding='UTF-8'?>\n", "")


def pdf_escape(value: str) -> str:
    parts: list[str] = []
    for byte in value.encode("utf-8"):
        if byte in (40, 41, 92):
            parts.append("\\" + chr(byte))
        elif 32 <= byte <= 126:
            parts.append(chr(byte))
        else:
            parts.append(f"\\{byte:03o}")
    return "".join(parts)


def _pdf_text(content: list[str], x: float, y: float, value: str, size: int = 10) -> None:
    content.append(f"BT /F1 {size} Tf {x:.2f} {y:.2f} Td ({pdf_escape(value)}) Tj ET")


def _pdf_qr(content: list[str], value: str, x: float, y: float, size: float) -> None:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=2,
    )
    qr.add_data(value)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    modules = len(matrix)
    module = size / modules
    content.append("0 0 0 rg")
    for row_index, row in enumerate(matrix):
        for col_index, dark in enumerate(row):
            if not dark:
                continue
            px = x + col_index * module
            py = y + (modules - row_index - 1) * module
            content.append(f"{px:.2f} {py:.2f} {module + 0.01:.2f} {module + 0.01:.2f} re f")


def _pdf_page_stream(payloads: list[LabelPayload]) -> bytes:
    content: list[str] = ["1 1 1 rg 0 0 612 792 re f", "0 0 0 RG", "0 0 0 rg"]
    card_w = 270.0
    card_h = 132.0
    gap_x = 18.0
    gap_y = 12.0
    start_x = 27.0
    start_y = 792.0 - 36.0 - card_h
    for index, payload in enumerate(payloads):
        col = index % 2
        row = index // 2
        x = start_x + col * (card_w + gap_x)
        y = start_y - row * (card_h + gap_y)
        loc = payload.location
        title = zpl_escape(loc.label or loc.id)
        path = " > ".join(zpl_escape(node.label or node.id) for node in payload.path)
        contents = "; ".join(item.name for item in payload.contents[:4]) if payload.contents else "Empty"
        tags = ", ".join(loc.purpose_tags[:4])
        content.append(f"0.2 w {x:.2f} {y:.2f} {card_w:.2f} {card_h:.2f} re S")
        _pdf_text(content, x + 10, y + card_h - 22, title[:48], 14)
        for offset, line in enumerate(wrap_lines(path, width=42, limit=2)):
            _pdf_text(content, x + 10, y + card_h - 42 - offset * 12, line, 9)
        for offset, line in enumerate(wrap_lines(contents, width=42, limit=3)):
            _pdf_text(content, x + 10, y + card_h - 70 - offset * 11, line, 9)
        if tags:
            _pdf_text(content, x + 10, y + 22, tags[:46], 8)
        _pdf_text(content, x + 10, y + 10, f"ID: {loc.id}"[:54], 7)
        _pdf_qr(content, payload.url, x + card_w - 100, y + 18, 82)
        _pdf_text(content, x + card_w - 100, y + 8, "Scan for contents", 7)
        _pdf_text(content, x + card_w - 100, y + 2, payload.url, 3)
    return "\n".join(content).encode("ascii")


def _pdf_document(page_streams: list[bytes]) -> bytes:
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_numbers = [4 + index * 2 for index in range(len(page_streams))]
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_streams)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, stream in enumerate(page_streams):
        page_obj = 4 + index * 2
        stream_obj = page_obj + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {stream_obj} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def render_label_sheet_pdf(payloads: list[LabelPayload]) -> bytes:
    pages = [payloads[index : index + 10] for index in range(0, len(payloads), 10)]
    if not pages:
        pages = [[]]
    return _pdf_document([_pdf_page_stream(page) for page in pages])


def _item_label_card(
    content: list[str],
    payload: ItemLabelPayload,
    *,
    x: float,
    y: float,
) -> None:
    item = payload.item
    locations = "; ".join(
        (
            f"{payload.location_paths.get(row.bin_id, row.bin_id)} "
            f"{row.quantity:g} {row.unit}"
        ).strip()
        for row in payload.rows[:4]
    )
    content.append(f"0.2 w {x:.2f} {y:.2f} 270 162 re S")
    _pdf_text(content, x + 12, y + 140, item.name[:42], 16)
    if item.category:
        _pdf_text(content, x + 12, y + 122, item.category[:48], 9)
    for offset, line in enumerate(wrap_lines(locations or "No recorded locations", width=34, limit=3)):
        _pdf_text(content, x + 12, y + 100 - offset * 12, line, 9)
    if item.aliases:
        aliases = ", ".join(item.aliases[:4])
        _pdf_text(content, x + 12, y + 30, aliases[:44], 8)
    _pdf_text(content, x + 12, y + 14, f"Item ID: {item.item_id}", 7)
    _pdf_qr(content, payload.url, x + 180, y + 40, 72)
    _pdf_text(content, x + 180, y + 28, "Scan item", 7)
    _pdf_text(content, x + 180, y + 20, payload.url, 3)


def render_item_label_sheet_pdf(payloads: list[ItemLabelPayload]) -> bytes:
    pages = [payloads[index : index + 8] for index in range(0, len(payloads), 8)]
    if not pages:
        pages = [[]]
    page_streams: list[bytes] = []
    for page in pages:
        content: list[str] = [
            "1 1 1 rg 0 0 612 792 re f",
            "0 0 0 RG",
            "0 0 0 rg",
        ]
        for index, payload in enumerate(page):
            _item_label_card(
                content,
                payload,
                x=27 + (index % 2) * 288,
                y=598 - (index // 2) * 178,
            )
        page_streams.append("\n".join(content).encode("ascii"))
    return _pdf_document(page_streams)


def render_item_label_pdf(payload: ItemLabelPayload) -> bytes:
    content: list[str] = ["1 1 1 rg 0 0 306 198 re f", "0 0 0 RG", "0 0 0 rg"]
    _item_label_card(content, payload, x=18, y=18)
    stream = "\n".join(content).encode("ascii")
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 306 198] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def _inventory_report_page(
    rows: list[InventoryReportRow],
    *,
    page_number: int,
    page_count: int,
    total_records: int,
    unique_items: int,
    generated_label: str,
) -> bytes:
    content: list[str] = ["1 1 1 rg 0 0 612 792 re f", "0 0 0 RG", "0 0 0 rg"]
    _pdf_text(content, 36, 754, "Magpie inventory", 20)
    _pdf_text(
        content,
        36,
        734,
        f"{unique_items} unique items across {total_records} location records",
        9,
    )
    if generated_label:
        _pdf_text(content, 430, 754, generated_label[:34], 8)
    content.append("0.4 w 36 706 540 1 re f")
    _pdf_text(content, 40, 714, "ITEM", 8)
    _pdf_text(content, 238, 714, "QUANTITY", 8)
    _pdf_text(content, 320, 714, "LOCATION", 8)
    y = 686.0
    for row in rows:
        title = zpl_escape(row.name)[:31]
        category = zpl_escape(row.category)[:31]
        quantity = f"{row.quantity:g} {zpl_escape(row.unit)}".strip()[:16]
        path = zpl_escape(row.location_path or row.location_id)
        if not row.location_active:
            path = f"{path} [archived]"
        _pdf_text(content, 40, y, title, 10)
        if category:
            _pdf_text(content, 40, y - 11, category, 7)
        _pdf_text(content, 238, y, quantity, 9)
        for offset, line in enumerate(wrap_lines(path, width=42, limit=2)):
            _pdf_text(content, 320, y - offset * 11, line, 8)
        content.append(f"0.85 0.85 0.85 RG 36 {y - 16:.2f} 540 0.3 re f 0 0 0 RG")
        y -= 29
    _pdf_text(content, 36, 28, f"Page {page_number} of {page_count}", 8)
    _pdf_text(content, 455, 28, "Magpie storage report", 8)
    return "\n".join(content).encode("ascii")


def render_inventory_report_pdf(
    rows: list[InventoryReportRow], *, generated_label: str = ""
) -> bytes:
    ordered = sorted(
        rows,
        key=lambda row: (
            row.location_path.casefold(),
            row.name.casefold(),
            row.item_id,
        ),
    )
    pages = [ordered[index : index + 22] for index in range(0, len(ordered), 22)] or [[]]
    unique_items = len({row.item_id for row in ordered})
    streams = [
        _inventory_report_page(
            page,
            page_number=index,
            page_count=len(pages),
            total_records=len(ordered),
            unique_items=unique_items,
            generated_label=generated_label,
        )
        for index, page in enumerate(pages, start=1)
    ]
    return _pdf_document(streams)


def render_location_zpl(payload: LabelPayload, cfg: ZebraConfig) -> str:
    loc = payload.location
    title = zpl_escape(loc.label or loc.id).upper()
    path = " > ".join(zpl_escape(p.label or p.id) for p in payload.path)
    contents = payload.contents[:5]
    content_text = "; ".join(item.name for item in contents) if contents else "Empty"
    tag_text = ", ".join(loc.purpose_tags[:5])

    width = cfg.width_dots
    length = cfg.length_dots
    scale = max(1.0, min(1.5, cfg.dpi / 203))
    title_size = int(54 * scale)
    path_size = int(30 * scale)
    tag_size = int(27 * scale)
    heading_size = int(34 * scale)
    body_size = int(31 * scale)
    footer_size = int(22 * scale)
    qr_mag = 7 if cfg.dpi >= 300 else 6
    qr_width = 310 if cfg.dpi >= 300 else 235
    qr_x = max(width - qr_width, 20)
    text_width = max(22, int((qr_x - 60) / max(body_size * 0.58, 1)))
    y = 46
    parts = [
        "^XA",
        "^CI28",
        f"^PW{width}",
        f"^LL{length}",
        "^LH0,0",
        "^FO24,36^GB{0},3,3^FS".format(max(width - 48, 200)),
    ]
    for line in wrap_lines(title, width=max(12, int(text_width * 0.62)), limit=2):
        parts.append(f"^FO24,{y}^A0N,{title_size},{title_size}^FD{line}^FS")
        y += title_size + 7
    y += 5
    for line in wrap_lines(path, width=text_width, limit=2):
        parts.append(f"^FO24,{y}^A0N,{path_size},{path_size}^FD{line}^FS")
        y += path_size + 6
    if tag_text:
        for line in wrap_lines(tag_text, width=text_width, limit=2):
            parts.append(f"^FO24,{y}^A0N,{tag_size},{tag_size}^FD{line}^FS")
            y += tag_size + 6
    y += 10
    parts.append(f"^FO24,{y}^A0N,{heading_size},{heading_size}^FDContains:^FS")
    y += heading_size + 9
    for line in wrap_lines(content_text, width=text_width, limit=4):
        parts.append(f"^FO38,{y}^A0N,{body_size},{body_size}^FD{line}^FS")
        y += body_size + 8
    id_y = max(length - (footer_size + 24), y + 12)
    parts.extend(
        [
            f"^FO24,{id_y}^A0N,{footer_size},{footer_size}^FDID: {zpl_escape(loc.id)[:36]}^FS",
            f"^FO{qr_x},90^BQN,2,{qr_mag}^FDLA,{zpl_escape(payload.url)}^FS",
            f"^FO{qr_x},340^A0N,{footer_size},{footer_size}^FDScan for contents^FS",
            "^XZ",
        ]
    )
    return "\n".join(parts) + "\n"


def send_zpl(zpl: str, cfg: ZebraConfig) -> None:
    if not cfg.host:
        raise ValueError("zebra host is not configured")
    with socket.create_connection((cfg.host, cfg.port), timeout=cfg.timeout_seconds) as sock:
        sock.sendall(zpl.encode("utf-8"))


def query_zebra_status(cfg: ZebraConfig) -> dict[str, object]:
    if not cfg.host:
        return {"ok": False, "error": "zebra host is not configured", "configured": asdict(cfg)}
    vars_to_query = [
        "device.product_name",
        "device.unique_id",
        "head.resolution.in_dpi",
        "ezpl.print_width",
        "zpl.label_length",
        "media.type",
    ]
    values: dict[str, str] = {}
    try:
        with socket.create_connection((cfg.host, cfg.port), timeout=cfg.timeout_seconds) as sock:
            sock.settimeout(cfg.timeout_seconds)
            for name in vars_to_query:
                sock.sendall(f'! U1 getvar "{name}"\r\n'.encode("ascii"))
                values[name] = _read_sgd_line(sock)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "configured": asdict(cfg), "values": values}
    return {"ok": True, "configured": asdict(cfg), "values": values}


def _read_sgd_line(sock: socket.socket) -> str:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = sock.recv(1024)
        except socket.timeout:
            break
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    return raw
