import requests
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)
MEILI_SEARCH_URL = "http://127.0.0.1:7700/indexes/pages/search"

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Moderní Sémantický Vyhledávač</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: system-ui, -apple-system, sans-serif; background: #f8f9fa; margin: 0; padding: 0; color: #202124; }
        .header { background: white; padding: 25px 5%; border-bottom: 1px solid #ebebeb; display: flex; flex-direction: column; align-items: center; position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 5px rgba(0,0,0,0.03); }
        .logo { font-size: 2.2rem; font-weight: 700; margin-bottom: 15px; color: #1a73e8; text-decoration: none; letter-spacing: -1px; }
        
        .search-container { width: 100%; max-width: 650px; position: relative; }
        .search-box { width: 100%; display: flex; gap: 10px; }
        input[type="text"] { width: 100%; padding: 14px 20px; font-size: 1.05rem; border: 1px solid #dfe1e5; border-radius: 28px; outline: none; box-shadow: 0 1px 6px rgba(32,33,36,0.1); }
        input[type="text"]:focus { border-color: #1a73e8; box-shadow: 0 1px 8px rgba(26,115,232,0.2); }
        
        /* Našeptávač */
        .autocomplete-items { position: absolute; border: 1px solid #dfe1e5; border-top: none; z-index: 99; top: 100%; left: 20px; right: 20px; background: white; border-bottom-left-radius: 12px; border-bottom-right-radius: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); overflow: hidden; }
        .autocomplete-items div { padding: 10px 15px; cursor: pointer; border-bottom: 1px solid #f1f3f4; font-size: 0.95rem; color: #202124; }
        .autocomplete-items div:hover { background-color: #f1f3f4; }

        /* Filtry */
        .filters-bar { width: 100%; max-width: 850px; margin: 15px auto 0 auto; display: flex; gap: 10px; padding: 0 15px; flex-wrap: wrap; }
        .filter-btn { background: white; border: 1px solid #dadce0; padding: 6px 14px; border-radius: 16px; font-size: 0.85rem; cursor: pointer; text-decoration: none; color: #5f6368; font-weight: 500; }
        .filter-btn.active, .filter-btn:hover { background: #e8f0fe; color: #1a73e8; border-color: #d2e3fc; }

        .container { max-width: 850px; margin: 20px auto; padding: 0 15px; }
        .stats { color: #70757a; font-size: 0.85rem; margin-bottom: 20px; }
        
        /* Výsledek */
        .result-item { background: white; padding: 22px; margin-bottom: 18px; border-radius: 12px; border: 1px solid #e0e0e0; display: flex; gap: 18px; box-shadow: 0 2px 4px rgba(0,0,0,0.02); transition: box-shadow 0.2s; }
        .result-item:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.06); }
        .result-content { flex: 1; }
        
        .badge-row { display: flex; gap: 8px; margin-bottom: 6px; align-items: center; }
        .badge { background: #e8f0fe; color: #1a73e8; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }
        .date-badge { color: #70757a; font-size: 0.8rem; }
        
        .result-title { font-size: 1.25rem; margin: 0 0 6px 0; font-weight: 500; }
        .result-title a { color: #1a0dab; text-decoration: none; }
        .result-title a:hover { text-decoration: underline; }
        .result-url { font-size: 0.85rem; color: #006621; margin-bottom: 8px; word-break: break-all; }
        .result-snippet { font-size: 0.95rem; color: #4d5156; line-height: 1.6; }
        
        /* Rich Results & Audio */
        .rich-data { margin-top: 12px; padding-top: 10px; border-top: 1px dashed #eee; font-size: 0.85rem; color: #5f6368; display: flex; gap: 15px; flex-wrap: wrap; align-items: center; }
        .rating { color: #f4b400; font-weight: bold; }
        .price { color: #0f9d58; font-weight: bold; }
        .audio-player { width: 100%; margin-top: 8px; height: 36px; }
        
        .result-img { width: 110px; height: 110px; object-fit: cover; border-radius: 8px; flex-shrink: 0; background: #f1f3f4; }
    </style>
</head>
<body>
    <div class="header">
        <a href="/" class="logo">🧠 SmartSearch AI</a>
        <div class="search-container">
            <form action="/" method="get" class="search-box">
                <input type="text" id="searchInput" name="q" value="{{ query }}" placeholder="Zadejte hledaný výraz, téma nebo otázku..." autocomplete="off" autofocus required>
            </form>
            <div id="autocompleteList" class="autocomplete-items"></div>
        </div>
    </div>

    <div class="filters-bar">
        <a href="/?q={{ query }}" class="filter-btn {% if not f %}active{% endif %}">Vše</a>
        <a href="/?q={{ query }}&f=PodcastEpisode" class="filter-btn {% if f == 'PodcastEpisode' %}active{% endif %}">🎙️ Podcasty</a>
        <a href="/?q={{ query }}&f=Article" class="filter-btn {% if f == 'Article' %}active{% endif %}">📰 Články</a>
        <a href="/?q={{ query }}&audio=1" class="filter-btn {% if audio %}active{% endif %}">🔊 S audio přehrávačem</a>
    </div>

    <div class="container">
        {% if query %}
            <div class="stats">Nalezeno {{ total }} výsledků (za {{ processing_time }} ms)</div>
            
            {% for item in results %}
            <div class="result-item">
                {% if item.image %}
                    <img src="{{ item.image }}" class="result-img" onerror="this.style.display='none'">
                {% endif %}
                <div class="result-content">
                    <div class="badge-row">
                        {% if item.schema_type and item.schema_type != 'WebPage' %}
                            <span class="badge">{{ item.schema_type }}</span>
                        {% endif %}
                        {% if item.published_date %}
                            <span class="date-badge">📅 {{ item.published_date }}</span>
                        {% endif %}
                    </div>
                    
                    <div class="result-url">{{ item.url }}</div>
                    <h3 class="result-title"><a href="{{ item.url }}" target="_blank">{{ item.title }}</a></h3>
                    <div class="result-snippet">{{ item.text[:220] }}...</div>
                    
                    {% if item.has_audio and item.audio_url %}
                        <audio controls class="audio-player">
                            <source src="{{ item.audio_url }}" type="audio/mpeg">
                            Váš prohlížeč nepodporuje audio element.
                        </audio>
                    {% endif %}

                    {% if item.schema_details %}
                    <div class="rich-data">
                        {% if item.schema_details.rating %}
                            <span class="rating">★ {{ item.schema_details.rating }} / 5 ({{ item.schema_details.reviews or 0 }} recenzí)</span>
                        {% endif %}
                        {% if item.schema_details.price %}
                            <span class="price">Cena: {{ item.schema_details.price }} {{ item.schema_details.currency or 'Kč' }}</span>
                        {% endif %}
                        {% if item.schema_details.author %}
                            <span>✍️ Autor: {{ item.schema_details.author }}</span>
                        {% endif %}
                    </div>
                    {% endif %}
                </div>
            </div>
            {% endfor %}

            {% if not results %}
                <p style="text-align: center; color: #70757a; margin-top: 40px;">Pro výraz <b>"{{ query }}"</b> nebyly nalezeny žádné výsledky.</p>
            {% endif %}
        {% else %}
            <p style="text-align: center; color: #70757a; margin-top: 60px;">Vyhledávací index 500 nejnovějších položek je připraven.</p>
        {% endif %}
    </div>

    <script>
        // Živý našeptávač (Autocomplete)
        const input = document.getElementById("searchInput");
        const list = document.getElementById("autocompleteList");

        input.addEventListener("input", async function() {
            const val = this.value;
            list.innerHTML = "";
            if (!val || val.length < 2) return;

            try {
                const res = await fetch(`/autocomplete?q=${encodeURIComponent(val)}`);
                const data = await res.json();
                
                data.forEach(title => {
                    const item = document.createElement("div");
                    item.innerHTML = `<strong>${title.substr(0, val.length)}</strong>${title.substr(val.length)}`;
                    item.addEventListener("click", function() {
                        input.value = title;
                        list.innerHTML = "";
                        input.form.submit();
                    });
                    list.appendChild(item);
                });
            } catch(e) {}
        });

        document.addEventListener("click", function(e) {
            if (e.target !== input) { list.innerHTML = ""; }
        });
    </script>
</body>
</html>
'''

@app.route('/')
def search():
    query = request.args.get('q', '')
    f_filter = request.args.get('f', '')
    audio_filter = request.args.get('audio', '')
    
    results = []
    total = 0
    processing_time = 0

    if query:
        filters = []
        if f_filter:
            filters.append(f"schema_type = '{f_filter}'")
        if audio_filter:
            filters.append("has_audio = true")

        payload = {
            "q": query,
            "limit": 25,
            "sort": ["published_timestamp:desc"]
        }
        if filters:
            payload["filter"] = " AND ".join(filters)

        try:
            res = requests.post(MEILI_SEARCH_URL, json=payload).json()
            results = res.get('hits', [])
            total = res.get('estimatedTotalHits', len(results))
            processing_time = res.get('processingTimeMs', 0)
        except Exception:
            pass

    return render_template_string(
        HTML_TEMPLATE, 
        query=query, 
        results=results, 
        total=total, 
        processing_time=processing_time,
        f=f_filter,
        audio=audio_filter
    )

@app.route('/autocomplete')
def autocomplete():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    try:
        res = requests.post(MEILI_SEARCH_URL, json={"q": query, "limit": 5}).json()
        titles = [hit.get('title') for hit in res.get('hits', []) if 'title' in hit]
        return jsonify(titles)
    except Exception:
        return jsonify([])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8095)
