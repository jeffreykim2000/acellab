# Acel Lab — public marketing site

Static HTML/CSS/JS for the **company** site (home, contact, careers, legal).

| Folder | Contents |
|--------|----------|
| `html/` | Pages: `index.html`, `contact.html`, `careers.html`, `privacy.html`, `terms.html` |
| `css/` | `style.css`, `careers.css` |
| `js/` | `i18n.js` |
| `images/`, `videos/` | Local assets |

**Startline** product marketing is in **`../startline_public/`**.  
The logged-in app is **`../platform/`** (Next.js).

### Preview

Serve the parent folder so `../` links to the other site work, e.g. from `acellab/acellab/`:

```bash
npx serve .
```

Then open `acellab_public/html/index.html` (or use the served tree).
