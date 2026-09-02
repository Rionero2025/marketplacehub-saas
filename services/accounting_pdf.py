from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from xml.sax.saxutils import escape

from services.accounting import computed_values, totals
from services.cecotec_orders import clean_text
from services.profit_sharing import normalized_percentages, split_profit


ITALIAN_MONTH_NAMES = (
    "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre",
)

MARKETPLACE_LABELS = {
    "kaufland": "Kaufland",
    "worten": "Worten",
}


@dataclass(frozen=True)
class AccountingPdfPeriod:
    mode: str
    label: str
    start: date | None = None
    end: date | None = None
    months: tuple[str, ...] = ()
    years: tuple[int, ...] = ()

    def contains(self, value: date) -> bool:
        if self.months:
            return month_key(value) in self.months
        if self.years:
            return value.year in self.years
        if self.start is not None and value < self.start:
            return False
        if self.end is not None and value > self.end:
            return False
        return True


def record_date(value: Any) -> date | None:
    """Return the accounting calendar date used by the on-screen order table."""
    text = clean_text(value)
    if not text:
        return None
    candidate = text[:10]
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            return None


def month_key(value: date | datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def month_label(value: str) -> str:
    try:
        year, month = (int(part) for part in str(value).split("-", 1))
        return f"{ITALIAN_MONTH_NAMES[month - 1]} {year}"
    except (TypeError, ValueError, IndexError):
        return clean_text(value)


def available_accounting_periods(records: Iterable[Mapping[str, Any]]) -> dict[str, list[Any]]:
    dates = [parsed for item in records if (parsed := record_date(item.get("order_created")))]
    months = sorted({month_key(item) for item in dates}, reverse=True)
    years = sorted({item.year for item in dates}, reverse=True)
    return {"months": months, "years": years}


def _month_bounds(value: str) -> tuple[date, date]:
    year, month = (int(part) for part in str(value).split("-", 1))
    start = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return start, next_month - timedelta(days=1)


def build_accounting_pdf_period(
    mode: str,
    *,
    reference_date: date | None = None,
    selected_day: date | None = None,
    selected_months: Sequence[str] = (),
    selected_years: Sequence[int] = (),
    custom_from: date | None = None,
    custom_to: date | None = None,
) -> AccountingPdfPeriod:
    today = reference_date or date.today()
    normalized_mode = clean_text(mode).lower()

    if normalized_mode == "day":
        chosen = selected_day or today
        return AccountingPdfPeriod("day", f"Giorno {chosen:%d/%m/%Y}", chosen, chosen)

    if normalized_mode == "current_week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        return AccountingPdfPeriod(
            "current_week", f"Settimana corrente {start:%d/%m/%Y} - {end:%d/%m/%Y}", start, end
        )

    if normalized_mode == "current_month":
        key = month_key(today)
        start, end = _month_bounds(key)
        return AccountingPdfPeriod(
            "current_month", f"Mese corrente - {month_label(key)}", start, end
        )

    if normalized_mode == "select_month":
        months = tuple(sorted({clean_text(item) for item in selected_months if clean_text(item)}))
        if not months:
            raise ValueError("Seleziona almeno un mese.")
        bounds = [_month_bounds(item) for item in months]
        labels = [month_label(item) for item in months]
        description = labels[0] if len(labels) == 1 else ", ".join(labels)
        return AccountingPdfPeriod(
            "select_month",
            f"Mesi selezionati - {description}",
            min(item[0] for item in bounds),
            max(item[1] for item in bounds),
            months=months,
        )

    if normalized_mode == "current_year":
        start = date(today.year, 1, 1)
        end = date(today.year, 12, 31)
        return AccountingPdfPeriod(
            "current_year", f"Anno corrente {today.year}", start, end
        )

    if normalized_mode == "select_year":
        years = tuple(sorted({int(item) for item in selected_years}, reverse=True))
        if not years:
            raise ValueError("Seleziona almeno un anno.")
        description = str(years[0]) if len(years) == 1 else ", ".join(map(str, years))
        return AccountingPdfPeriod(
            "select_year",
            f"Anni selezionati - {description}",
            date(min(years), 1, 1),
            date(max(years), 12, 31),
            years=years,
        )

    if normalized_mode == "custom":
        start = custom_from or today
        end = custom_to or start
        if start > end:
            raise ValueError("La data iniziale non può essere successiva alla data finale.")
        return AccountingPdfPeriod(
            "custom", f"Intervallo personalizzato {start:%d/%m/%Y} - {end:%d/%m/%Y}", start, end
        )

    raise ValueError(f"Periodo PDF non supportato: {mode}")


def filter_accounting_records(
    records: Iterable[Mapping[str, Any]],
    period: AccountingPdfPeriod,
    marketplaces: Sequence[str] = (),
) -> list[dict[str, Any]]:
    allowed_marketplaces = {
        clean_text(item).lower() for item in marketplaces if clean_text(item)
    }
    output: list[dict[str, Any]] = []
    for source in records:
        item = dict(source)
        marketplace = clean_text(item.get("marketplace")).lower()
        if allowed_marketplaces and marketplace not in allowed_marketplaces:
            continue
        created = record_date(item.get("order_created"))
        if created is None or not period.contains(created):
            continue
        output.append(item)
    output.sort(
        key=lambda item: (
            record_date(item.get("order_created")) or date.min,
            clean_text(item.get("marketplace")),
            clean_text(item.get("order_id")),
            clean_text(item.get("order_line_id")),
        )
    )
    return output


def _safe_file_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", clean_text(value)).strip("_")


def accounting_pdf_file_name(
    marketplaces: Sequence[str],
    period: AccountingPdfPeriod,
) -> str:
    labels = [MARKETPLACE_LABELS.get(clean_text(item).lower(), clean_text(item).title()) for item in marketplaces]
    market_part = "_".join(_safe_file_part(item) for item in labels if item) or "Marketplace"
    if period.months:
        period_part = "mesi_" + "_".join(period.months)
    elif period.years:
        period_part = "anni_" + "_".join(str(item) for item in period.years)
    elif period.start and period.end:
        period_part = f"{period.start:%Y%m%d}_{period.end:%Y%m%d}"
    else:
        period_part = "periodo"
    return f"Contabilita_PDF_{market_part}_{period_part}.pdf"


def _money(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", ".")


def _number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _paragraph(text: Any, style):
    from reportlab.platypus import Paragraph

    return Paragraph(escape(clean_text(text)), style)


def _font_configuration() -> tuple[str, str]:
    """Register a Unicode font when available, without bundling font files."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        (
            "MarketplaceHubSans",
            "MarketplaceHubSans-Bold",
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf"),
        ),
        (
            "MarketplaceHubSans",
            "MarketplaceHubSans-Bold",
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
        (
            "MarketplaceHubSans",
            "MarketplaceHubSans-Bold",
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        ),
    ]
    for regular_name, bold_name, regular_path, bold_path in candidates:
        if not regular_path.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(regular_name, str(regular_path)))
            if bold_path.exists():
                pdfmetrics.registerFont(TTFont(bold_name, str(bold_path)))
            else:
                bold_name = regular_name
            return regular_name, bold_name
        except Exception:
            continue
    return "Helvetica", "Helvetica-Bold"


def _summary_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    our_profit_pct: float,
    partner_profit_pct: float,
    partner_name: str,
) -> list[list[str]]:
    summary = totals(records)
    shares = split_profit(summary["net_revenue"], our_profit_pct, partner_profit_pct)
    unique_orders = len({clean_text(item.get("order_id")) for item in records if clean_text(item.get("order_id"))})
    missing_costs = sum(_number(item.get("purchase_cost_eur")) is None for item in records)
    total_quantity = sum(max(1, int(_number(item.get("quantity")) or 1)) for item in records)
    return [
        ["Righe", f"{len(records):,}", "Ordini", f"{unique_orders:,}"],
        ["Quantità", f"{total_quantity:,}", "Costi da verificare", f"{missing_costs:,}"],
        ["Vendite", _money(summary["sale"]), "Commissioni", _money(summary["commission"])],
        ["Rimborsi", _money(summary["refund"]), "Da ricevere", _money(summary["payout"])],
        ["Acquisti", _money(summary["purchase"]), "Margine lordo", _money(summary["gross_margin"])],
        ["Margine utile", _money(summary["net_revenue"]), f"Nostra quota {our_profit_pct:g}%", _money(shares["our_amount"])],
        [f"Quota {partner_name} {partner_profit_pct:g}%", _money(shares["partner_amount"]), "", ""],
    ]


def accounting_pdf_bytes(
    records: Sequence[Mapping[str, Any]],
    *,
    seller_name: str,
    period: AccountingPdfPeriod,
    marketplaces: Sequence[str],
    our_profit_pct: float = 0.0,
    partner_profit_pct: float = 100.0,
    partner_name: str = "Partner",
    include_details: bool = True,
    generated_at: datetime | None = None,
) -> bytes:
    if not records:
        raise ValueError("Nessuna riga contabile disponibile per il periodo selezionato.")

    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            KeepTogether, LongTable, PageBreak, Paragraph, SimpleDocTemplate,
            Spacer, Table, TableStyle,
        )
    except ImportError as exc:  # pragma: no cover - depends on user installation
        raise RuntimeError(
            "Manca ReportLab. Esegui RIPARA_DIPENDENZE_WINDOWS.bat oppure installa reportlab."
        ) from exc

    our_pct, partner_pct = normalized_percentages(our_profit_pct, partner_profit_pct)
    partner_label = clean_text(partner_name) or "Partner"
    regular_font, bold_font = _font_configuration()
    generated = generated_at or datetime.now()
    selected_marketplaces = [clean_text(item).lower() for item in marketplaces if clean_text(item)]
    selected_marketplaces = list(dict.fromkeys(selected_marketplaces))

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="Documento contabile Marketplace Hub",
        author="Marketplace Hub",
        subject=period.label,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AccountingTitle", parent=styles["Title"], fontName=bold_font,
        fontSize=20, leading=24, textColor=colors.HexColor("#1f2937"),
        alignment=TA_LEFT, spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "AccountingSubtitle", parent=styles["Normal"], fontName=regular_font,
        fontSize=9, leading=12, textColor=colors.HexColor("#4b5563"),
        spaceAfter=4,
    )
    heading_style = ParagraphStyle(
        "AccountingHeading", parent=styles["Heading2"], fontName=bold_font,
        fontSize=13, leading=16, textColor=colors.HexColor("#1f4e78"),
        spaceBefore=7, spaceAfter=5,
    )
    body_style = ParagraphStyle(
        "AccountingBody", parent=styles["BodyText"], fontName=regular_font,
        fontSize=7, leading=8.5, textColor=colors.HexColor("#111827"),
    )
    body_center = ParagraphStyle(
        "AccountingBodyCenter", parent=body_style, alignment=TA_CENTER,
    )
    body_right = ParagraphStyle(
        "AccountingBodyRight", parent=body_style, alignment=TA_RIGHT,
    )
    tiny_style = ParagraphStyle(
        "AccountingTiny", parent=body_style, fontSize=6.2, leading=7.2,
    )
    table_header_style = ParagraphStyle(
        "AccountingTableHeader", parent=body_style, fontName=bold_font,
        textColor=colors.white, alignment=TA_CENTER, fontSize=6.4, leading=7.2,
    )

    market_display = ", ".join(
        MARKETPLACE_LABELS.get(item, item.title()) for item in selected_marketplaces
    ) or "Marketplace"
    story: list[Any] = [
        Paragraph("Documento contabile Marketplace", title_style),
        Paragraph(f"Seller: <b>{escape(clean_text(seller_name) or 'Seller')}</b>", subtitle_style),
        Paragraph(f"Marketplace: <b>{escape(market_display)}</b>", subtitle_style),
        Paragraph(f"Periodo: <b>{escape(period.label)}</b>", subtitle_style),
        Paragraph(f"Generato il {generated:%d/%m/%Y alle %H:%M}", subtitle_style),
        Spacer(1, 4 * mm),
        Paragraph("Riepilogo complessivo", heading_style),
    ]

    summary_table = Table(
        [[_paragraph(value, body_style) for value in row] for row in _summary_rows(
            records,
            our_profit_pct=our_pct,
            partner_profit_pct=partner_pct,
            partner_name=partner_label,
        )],
        colWidths=[42 * mm, 30 * mm, 49 * mm, 34 * mm],
        hAlign="LEFT",
    )
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular_font),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#dbeafe")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#dbeafe")),
        ("FONTNAME", (0, 0), (0, -1), bold_font),
        ("FONTNAME", (2, 0), (2, -1), bold_font),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b7c7d8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        "Documento gestionale interno. Gli importi derivano dai dati contabili memorizzati "
        "nel programma e non sostituiscono fatture, estratti conto o documenti fiscali.",
        subtitle_style,
    ))

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in records:
        key = clean_text(item.get("marketplace")).lower() or "marketplace"
        grouped.setdefault(key, []).append(item)

    market_order = selected_marketplaces + [key for key in grouped if key not in selected_marketplaces]
    market_order = [key for key in dict.fromkeys(market_order) if key in grouped]
    for group_index, marketplace in enumerate(market_order):
        group_records = grouped[marketplace]
        if group_index > 0 or include_details:
            story.append(PageBreak())
        market_label_text = MARKETPLACE_LABELS.get(marketplace, marketplace.title())
        story.append(Paragraph(f"{escape(market_label_text)} - riepilogo", heading_style))
        market_summary = Table(
            [[_paragraph(value, body_style) for value in row] for row in _summary_rows(
                group_records,
                our_profit_pct=our_pct,
                partner_profit_pct=partner_pct,
                partner_name=partner_label,
            )],
            colWidths=[42 * mm, 30 * mm, 49 * mm, 34 * mm],
            hAlign="LEFT",
        )
        market_summary.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e2e8f0")),
            ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#e2e8f0")),
            ("FONTNAME", (0, 0), (-1, -1), regular_font),
            ("FONTNAME", (0, 0), (0, -1), bold_font),
            ("FONTNAME", (2, 0), (2, -1), bold_font),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b7c7d8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(market_summary)

        if not include_details:
            continue

        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(f"Dettaglio righe - {escape(market_label_text)}", heading_style))
        header_values = [
            "Data", "Ordine", "Market", "Fornitore", "Prodotto", "EAN", "Q.tà",
            "Vendita", "Acquisto", "Comm.", "Da ricevere", "Margine", "Stato",
        ]
        table_data: list[list[Any]] = [
            [Paragraph(escape(value), table_header_style) for value in header_values]
        ]
        row_tones: list[str] = []
        for item in group_records:
            created = record_date(item.get("order_created"))
            computed = computed_values(item)
            net_margin = computed.get("net_revenue_eur")
            status = clean_text(item.get("status_label"))
            table_data.append([
                _paragraph(f"{created:%d/%m/%Y}" if created else "", body_center),
                _paragraph(item.get("order_id"), tiny_style),
                _paragraph(item.get("market_label") or market_label_text, tiny_style),
                _paragraph(item.get("supplier"), tiny_style),
                _paragraph(item.get("product_title"), tiny_style),
                _paragraph(item.get("ean"), tiny_style),
                _paragraph(max(1, int(_number(item.get("quantity")) or 1)), body_center),
                _paragraph(_money(item.get("sale_eur")), body_right),
                _paragraph(
                    "Da verificare" if _number(item.get("purchase_cost_eur")) is None
                    else _money(item.get("purchase_cost_eur")), body_right,
                ),
                _paragraph(_money(item.get("commission_eur")), body_right),
                _paragraph(_money(item.get("payout_eur")), body_right),
                _paragraph("-" if net_margin is None else _money(net_margin), body_right),
                _paragraph(status, tiny_style),
            ])
            status_lower = status.casefold()
            if any(token in status_lower for token in ("cancell", "annull", "no stock")):
                row_tones.append("cancelled")
            elif _number(item.get("purchase_cost_eur")) is None:
                row_tones.append("missing")
            elif net_margin is not None and float(net_margin) < 0:
                row_tones.append("loss")
            else:
                row_tones.append("normal")

        detail_table = LongTable(
            table_data,
            repeatRows=1,
            colWidths=[
                16 * mm, 26 * mm, 18 * mm, 21 * mm, 47 * mm, 25 * mm, 8 * mm,
                17 * mm, 17 * mm, 17 * mm, 17 * mm, 17 * mm, 24 * mm,
            ],
            hAlign="LEFT",
        )
        detail_style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e78")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), bold_font),
            ("FONTNAME", (0, 1), (-1, -1), regular_font),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
            ("TOPPADDING", (0, 0), (-1, -1), 2.2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
        ]
        for index, tone in enumerate(row_tones, start=1):
            if tone == "cancelled":
                background = colors.HexColor("#f3f4f6")
            elif tone == "missing":
                background = colors.HexColor("#fef3c7")
            elif tone == "loss":
                background = colors.HexColor("#fee2e2")
            elif index % 2 == 0:
                background = colors.HexColor("#f8fafc")
            else:
                continue
            detail_style.append(("BACKGROUND", (0, index), (-1, index), background))
        detail_table.setStyle(TableStyle(detail_style))
        story.append(detail_table)

    footer_text = (
        f"{clean_text(seller_name) or 'Seller'} - {period.label} - "
        "Documento gestionale Marketplace Hub"
    )

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(regular_font, 7)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(12 * mm, 7 * mm, footer_text[:160])
        canvas.drawRightString(
            landscape(A4)[0] - 12 * mm,
            7 * mm,
            f"Pagina {doc.page}",
        )
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
