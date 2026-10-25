"""Synthetic hard cases for the redaction test campaign (run in the pdf-redactor-terminal image: PyMuPDF)."""
import sys
import pymupdf

OUT = sys.argv[1]
NIMBUS = "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf"


def qr_png(seed=3):
    qr = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 120, 120), False)
    qr.clear_with(255)
    for y in range(0, 120, 8):
        for x in range(0, 120, 8):
            if (x * 7 + y * 13 + seed) % 3 == 0:
                qr.set_rect(pymupdf.IRect(x, y, x + 8, y + 8), (0,))
    return qr.tobytes("png")


def text_png(text, size=26, w=320, h=70):
    src = pymupdf.open()
    p = src.new_page(width=w, height=h)
    p.insert_text((10, h * 0.65), text, fontsize=size, fontname="hebo")
    return p.get_pixmap(dpi=150).tobytes("png")


# S1: hidden places - invisible text, white text, text under an image, 1pt text, hidden OCG layer,
#     metadata, XMP, bookmark, annotation, attachment, form field, JavaScript, link
def s1():
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 60), "Vertrag mit Karl Brenner, Kundennummer: KN-55-90311", fontsize=11)
    p.insert_text((50, 80), "Ansprechpartnerin: Petra Lindqvist, petra.lindqvist@beispiel.de", fontsize=11)
    p.insert_text((50, 100), "Dieser Absatz bleibt sichtbar und lesbar.", fontsize=11)
    p.insert_text((50, 140), "Karl Brenner (unsichtbar)", fontsize=11, render_mode=3)          # invisible text
    p.insert_text((50, 160), "Karl Brenner weiss auf weiss", fontsize=11, color=(1, 1, 1))     # white text
    p.insert_text((60, 200), "Karl Brenner unter Bild", fontsize=11)                          # covered by image
    p.insert_image(pymupdf.Rect(50, 185, 300, 215), stream=text_png("Logo", 20, 300, 40))
    p.insert_text((50, 240), "Karl Brenner winzig", fontsize=1)                               # 1 pt text
    ocg = doc.add_ocg("Versteckt", on=False)
    p.insert_text((50, 260), "Petra Lindqvist versteckte Ebene", fontsize=11, oc=ocg)         # hidden layer
    doc.set_metadata({"title": "Akte Karl Brenner", "author": "Petra Lindqvist", "subject": "KN-55-90311",
                      "keywords": "Brenner Lindqvist"})
    doc.set_xml_metadata("<x:xmpmeta xmlns:x='adobe:ns:meta/'><dc>Karl Brenner</dc></x:xmpmeta>")
    doc.set_toc([[1, "Karl Brenner", 1]])
    p.add_text_annot((400, 300), "Rueckruf Petra Lindqvist")
    p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 70, 300, 85),
                   "uri": "mailto:petra.lindqvist@beispiel.de"})
    doc.embfile_add("notiz.txt", b"Karl Brenner KN-55-90311 petra.lindqvist@beispiel.de")
    w = pymupdf.Widget()
    w.field_name, w.field_type, w.field_value = "kunde", pymupdf.PDF_WIDGET_TYPE_TEXT, "Karl Brenner"
    w.rect = pymupdf.Rect(50, 400, 250, 420)
    p.add_widget(w)
    js = doc.get_new_xref()
    doc.update_object(js, "<< /S /JavaScript /JS (app.alert('Karl Brenner')) >>")
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{js} 0 R")
    doc.save(f"{OUT}/s1_hidden_places.pdf")


# S2: multi-page ticket set - names in text and in images, barcodes, labels with different numbers
def s2():
    doc = pymupdf.open()
    people = [("Miriam", "Okonkwo-Hansen"), ("Miriam", "Okonkwo-Hansen"), ("Tobias", "Weißgerber"),
              ("Miriam", "Okonkwo-Hansen"), ("Élodie", "Fournier")]
    boiler = ["Wichtige Hinweise: Die Fahrkarte gilt nur mit amtlichem Lichtbildausweis.",
              "Bei BahnCard-Rabatt bitte die BahnCard vorzeigen.", "Eine Stornierung ist ausgeschlossen."] * 4
    for n, (fn, ln) in enumerate(people):
        p = doc.new_page()
        p.insert_text((50, 60), f"Online-Ticket   Auftragsnummer: {600123450 + n * 7919}", fontsize=13, fontname="hebo")
        p.insert_text((50, 85), f"Reisende/r: {fn} {ln}", fontsize=11)
        p.insert_text((50, 100), f"Kundennummer: KD-{88000 + n * 13}", fontsize=11)
        p.insert_text((50, 115), f"Ticketcode: {['QX4TB8LM', 'HW2PZ7KR', 'NV6CJ3DS', 'TL9GW5EB', 'MZ7QA2HU'][n]}", fontsize=11)
        p.insert_text((50, 135), "Berlin Hbf -> Köln Hbf, ICE 1001, 2. Klasse, 49,90 EUR", fontsize=11)
        for k, t in enumerate(boiler):
            p.insert_text((50, 170 + k * 13), t, fontsize=9)
        p.insert_image(pymupdf.Rect(150, 620, 470, 690), stream=text_png(f"{fn} {ln}", 22))
        p.insert_image(pymupdf.Rect(420, 30, 540, 150), stream=qr_png(n))
    doc.save(f"{OUT}/s2_ticket_set.pdf")


# S3: tricky text - umlauts/hyphens/caps, line break inside a name, letter-spaced, rotated 90°, table, ligature
def s3():
    doc = pymupdf.open()
    p = doc.new_page()
    y = 60
    for ln in ["Sachbearbeiter: Jürgen Müller-Lüdenscheidt", "Zeuge: JÜRGEN MÜLLER-LÜDENSCHEIDT (Großschreibung)",
               "Rückfrage bei Frau Dr. Ayşe Yılmaz-Öztürk, Tel. 0221 9876543", "Ein Satz ohne Namen bleibt stehen.",
               "Geschäftsführer Jürgen"]:
        p.insert_text((50, y), ln, fontsize=11, fontname="nimbus", fontfile=NIMBUS)
        y += 18
    p.insert_text((50, y), "Müller-Lüdenscheidt unterschreibt.", fontsize=11, fontname="nimbus", fontfile=NIMBUS)  # name split over two lines (prev+this)
    y += 30
    x = 50
    for ch in "Jürgen Müller-Lüdenscheidt":                                      # letter-spaced, one char per op
        p.insert_text((x, y), ch, fontsize=11, fontname="nimbus", fontfile=NIMBUS)
        x += 9
    p.insert_text((520, 400), "Ayşe Yılmaz-Öztürk", fontsize=11, rotate=90, fontname="nimbus", fontfile=NIMBUS)       # vertical text
    tw = pymupdf.TextWriter(p.rect)
    font = pymupdf.Font(fontfile=NIMBUS)  # embedded -> Type0/CID
    tw.append((50, 300), "Unicode-Font: Ayşe Yılmaz-Öztürk, IBAN DE02 1203 0000 0000 2020 51", font=font, fontsize=10)
    tw.write_text(p)
    for r, (a, b) in enumerate([("Name", "Telefon"), ("Jürgen Müller-Lüdenscheidt", "+49 221 555 0101"),
                                ("Ayşe Yılmaz-Öztürk", "+49 221 555 0102")]):
        p.insert_text((50, 360 + r * 16), a, fontsize=10, fontname="nimbus", fontfile=NIMBUS)
        p.insert_text((260, 360 + r * 16), b, fontsize=10, fontname="nimbus", fontfile=NIMBUS)
    doc.save(f"{OUT}/s3_tricky_text.pdf")


# S4: landscape page with /Rotate 90 and an image containing a name + text
def s4():
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((50, 60), "Quittung für Sven Achterberg, Beleg-Nr. 2026-0815", fontsize=12)
    p.insert_text((50, 80), "Betrag 120,00 EUR dankend erhalten.", fontsize=12)
    p.insert_image(pymupdf.Rect(50, 120, 370, 190), stream=text_png("Sven Achterberg", 24))
    p.set_rotation(90)
    doc.save(f"{OUT}/s4_rotated_image.pdf")


for f in (s1, s2, s3, s4):
    f()
print("ok")


# S5: German invoice, 2 pages: logo image, customer block, numbers, VAT/tax ids, IBAN, contact, signature image,
#     bank footer on every page
def s5():
    doc = pymupdf.open()
    for n in range(2):
        p = doc.new_page()
        p.insert_image(pymupdf.Rect(50, 30, 250, 80), stream=text_png("Nordlicht Haustechnik GmbH", 20, 420, 70))
        if n == 0:
            for k, t in enumerate(["Herrn", "Jonas Albrecht", "Lindenallee 17", "50667 Köln"]):
                p.insert_text((50, 120 + k * 14), t, fontsize=11)
            for k, t in enumerate(["Rechnungsnummer: RE-2026-04711", "Kundennummer: K-88231", "Rechnungsdatum: 12.03.2026",
                                   "Ihre USt-IdNr.: DE287654321"]):
                p.insert_text((330, 120 + k * 14), t, fontsize=10)
            p.insert_text((50, 210), "Rechnung", fontsize=16, fontname="hebo")
            p.insert_text((50, 235), "Sehr geehrter Herr Albrecht, wir berechnen Ihnen folgende Leistungen:", fontsize=10)
            rows = [("Pos", "Leistung", "Menge", "Betrag"), ("1", "Wartung Heizungsanlage", "1", "189,00 EUR"),
                    ("2", "Austausch Umwälzpumpe", "1", "412,50 EUR"), ("3", "Anfahrt", "1", "45,00 EUR")]
            for r, row in enumerate(rows):
                for c, (x, t) in enumerate(zip((50, 90, 380, 450), row)):
                    p.insert_text((x, 265 + r * 16), t, fontsize=10, fontname="hebo" if r == 0 else "helv")
        else:
            p.insert_text((50, 120), "Rechnung RE-2026-04711, Seite 2", fontsize=11)
            p.insert_text((50, 150), "Summe netto 646,50 EUR, USt 19 % 122,84 EUR, Gesamt 769,34 EUR", fontsize=10)
            p.insert_text((50, 175), "Zahlbar bis 26.03.2026 ohne Abzug. Ihr Ansprechpartner: Mira Szymańska, Tel. 0221 4455667",
                          fontsize=10, fontname="nimbus", fontfile=NIMBUS)
            p.insert_text((50, 190), "E-Mail: m.szymanska@nordlicht-haustechnik.de", fontsize=10)
            p.insert_image(pymupdf.Rect(50, 205, 250, 245), stream=text_png("M. Szymanska", 22, 300, 60))
        p.insert_text((50, 800), "Nordlicht Haustechnik GmbH · Steuernr. 215/5871/3301 · USt-IdNr. DE312345678 · "
                      "IBAN DE44 5001 0517 5407 3249 31 · BIC INGDDEFFXXX", fontsize=7)
    doc.set_metadata({"title": "Rechnung RE-2026-04711 Jonas Albrecht", "author": "Mira Szymańska"})
    doc.save(f"{OUT}/s5_invoice_de.pdf")


# S6: scanned invoice, slightly skewed (1.5 deg), as a page image
def s6():
    src = pymupdf.open(f"{OUT}/s5_invoice_de.pdf")
    pix = src[0].get_pixmap(dpi=150, matrix=pymupdf.Matrix(1, 1).prerotate(1.5))
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_image(p.rect, pixmap=pix)
    doc.save(f"{OUT}/s6_invoice_scan.pdf")


# S7: medical letter (Arztbrief), 2 pages: patient data, inverted name in footer, ICD-10, medication, lab table,
#     doctor + signature image
def s7():
    doc = pymupdf.open()
    for n in range(2):
        p = doc.new_page()
        p.insert_text((50, 50), "Klinikum am Stadtpark · Klinik für Innere Medizin · Parkstraße 4 · 40210 Düsseldorf", fontsize=9)
        if n == 0:
            lines = ["Arztbrief", "Patient: Erika Musterfrau, geb. 14.02.1961", "Versichertennummer: A123456789",
                     "Fallnummer: 2026-778812", "Anschrift: Birkenweg 3, 40489 Düsseldorf", "Aufnahme: 02.03.2026, Entlassung: 09.03.2026",
                     "Diagnosen:", "- Akuter NSTEMI (I21.4)", "- Diabetes mellitus Typ 2 (E11.9 G)", "- Arterielle Hypertonie (I10.90)",
                     "Medikation: ASS 100 mg 1-0-0, Metformin 1000 mg 1-0-1, Ramipril 5 mg 1-0-0",
                     "Frau Musterfrau wurde in stabilem Zustand entlassen.", "Mit freundlichen Grüßen",
                     "Dr. med. Hannes Kröger-Wendt, Oberarzt"]
            for k, t in enumerate(lines):
                p.insert_text((50, 90 + k * 16), t, fontsize=12 if k == 0 else 10, fontname="hebo" if k == 0 else "helv")
            p.insert_image(pymupdf.Rect(50, 330, 230, 370), stream=text_png("H. Kröger-Wendt", 22, 300, 60))
        else:
            p.insert_text((50, 90), "Laborwerte vom 03.03.2026 (Patient: Musterfrau, Erika)", fontsize=10, fontname="hebo")
            for r, row in enumerate([("Parameter", "Wert", "Referenz"), ("Troponin T hs", "412 ng/l", "< 14"),
                                     ("HbA1c", "7,9 %", "< 6,5"), ("Kreatinin", "1,1 mg/dl", "0,5-0,9")]):
                for x, t in zip((50, 220, 340), row):
                    p.insert_text((x, 115 + r * 15), t, fontsize=10)
        p.insert_text((50, 810), f"Musterfrau, Erika * 14.02.1961 · Fall 2026-778812 · Seite {n + 1}/2", fontsize=8)
    doc.save(f"{OUT}/s7_arztbrief.pdf")


# S8: scanned lab result with a patient label sticker (image with name + birth date + barcode-like stripes)
def s8():
    src = pymupdf.open()
    p = src.new_page()
    p.insert_text((50, 60), "Labor Dr. Weiss & Partner · Befundbericht", fontsize=12, fontname="hebo")
    p.insert_text((50, 90), "Einsender: Praxis Dr. Lea Brandt", fontsize=10)
    for k, t in enumerate(["Blutbild vom 05.03.2026", "Leukozyten 7,2 /nl (4,0-10,0)", "Hämoglobin 13,1 g/dl (12-16)",
                           "CRP 2,4 mg/l (< 5)"]):
        p.insert_text((50, 200 + k * 16), t, fontsize=10)
    p.draw_rect(pymupdf.Rect(330, 80, 545, 150), color=(0, 0, 0), width=0.8)
    p.insert_text((340, 100), "Musterfrau, Erika", fontsize=11, fontname="hebo")
    p.insert_text((340, 116), "geb. 14.02.1961  w", fontsize=10)
    p.insert_text((340, 132), "KV-Nr. A123456789", fontsize=10)
    for x in range(340, 530, 4):
        p.draw_line((x, 136), (x, 146), width=1.5 if x % 8 else 0.6)
    pix = p.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
    doc = pymupdf.open()
    np_ = doc.new_page()
    np_.insert_image(np_.rect, pixmap=pix)
    doc.save(f"{OUT}/s8_lab_scan.pdf")


for f in (s5, s6, s7, s8):
    f()
print("ok2")
