"""QR codes for the sign-in poster, drawn on the server with segno (as two-factor set-up does)."""
import io

import segno


def svg(url, scale=10):
    """An inline <svg> (no XML declaration) for the poster. It's markup we generate, not user input."""
    buf = io.BytesIO()
    segno.make(url, error='m').save(buf, kind='svg', scale=scale, border=2, xmldecl=False, svgns=True,
                                    svgclass='sf-qr', title='Scan to sign in to this site', omitsize=True)
    return buf.getvalue().decode()
