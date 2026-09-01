#!/usr/bin/env python3
"""
Mini Search - Search UI v3.0
Optimized for Ubuntu 26.04 ARM64 + Termux environment
Lightweight version with fallback vector search backends

Flask-based search interface with vector search.
Runs on port 8095.
"""

import sqlite3
import json
import time
import numpy as np
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify
import crawler_engine

app = Flask(__name__)

# Check if vector backend is available
if not crawler_engine.vector_db:
    print("⚠️  Warning: Vector search backend is not available. Search will not work properly.")


def get_vector_results(query, limit=25, filters=None):
    """Search vector database with filters"""
    if not crawler_engine.vector_db:
        return []
    
    try:
        # Generate query embedding
        if crawler_engine.embedding_model:
            query_embedding = crawler_engine.embedding_model.encode([query]).tolist()[0]
        else:
            return []
        
        # Build filter query for metadata
        filtered_results = []
        
        # First get all results from vector search
        results = crawler_engine.vector_db.query(
            query_embeddings=[query_embedding],
            n_results=limit * 2,  # Get more to apply filters
            where=None,
            include=["metadatas", "distances"]
        )
        
        # Apply filters manually (since our lightweight backend doesn't support where clause)
        if filters:
            for i in range(len(results.get('ids', []))):
                metadata = results['metadatas'][0][i] if results.get('metadatas') else {}
                
                # Check filters
                match = True
                
                if 'schema_type' in filters:
                    schema_types = filters['schema_type']
                    if isinstance(schema_types, str):
                        schema_types = [schema_types]
                    if metadata.get('schema_type') not in schema_types:
                        match = False
                
                if match and filters.get('has_audio'):
                    if metadata.get('has_audio') != 'True':
                        match = False
                
                if match and filters.get('has_price'):
                    schema_details = metadata.get('schema_details', '')
                    try:
                        details = json.loads(schema_details) if isinstance(schema_details, str) else schema_details
                        if not details.get('price'):
                            match = False
                    except:
                        match = False
                
                if match:
                    filtered_results.append({
                        'id': results['ids'][0][i],
                        'metadata': metadata,
                        'distance': results['distances'][0][i] if results.get('distances') else 0
                    })
        else:
            for i in range(len(results.get('ids', []))):
                filtered_results.append({
                    'id': results['ids'][0][i],
                    'metadata': results['metadatas'][0][i] if results.get('metadatas') else {},
                    'distance': results['distances'][0][i] if results.get('distances') else 0
                })
        
        # Sort by distance (ascending)
        filtered_results.sort(key=lambda x: x['distance'])
        
        # Limit results
        filtered_results = filtered_results[:limit]
        
        # Calculate relevance score for each result
        for result in filtered_results:
            result['relevance'] = calculate_relevance_score(
                result['metadata'], 
                result['distance']
            )
        
        # Re-sort by relevance
        filtered_results.sort(key=lambda x: x['relevance'], reverse=True)
        
        return filtered_results
        
    except Exception as e:
        print(f"⚠️  Chyba při vyhledávání v vector DB: {e}")
        return []


def calculate_relevance_score(metadata, distance):
    """Calculate relevance score combining multiple factors"""
    # Base score from vector search (convert distance to similarity)
    # Distance is cosine distance, so similarity = 1 - distance
    similarity_score = 1.0 - distance
    
    # Recency bonus (newer = higher)
    published_timestamp = metadata.get('published_timestamp', 0)
    if published_timestamp > 0:
        age_hours = (time.time() - published_timestamp) / 3600
        # Newer items get higher bonus (up to 0.3 for very recent)
        recency_bonus = max(0, 0.3 * (1 - min(1, age_hours / 720)))  # 720 hours = 30 days
    else:
        recency_bonus = 0
    
    # Rich data bonus
    rich_bonus = 0
    schema_type = metadata.get('schema_type', '')
    schema_details = metadata.get('schema_details', '')
    
    try:
        details = json.loads(schema_details) if isinstance(schema_details, str) else schema_details
        if isinstance(details, dict):
            if details.get('rating'):
                rich_bonus += 0.1
            if details.get('price'):
                rich_bonus += 0.1
            if details.get('author'):
                rich_bonus += 0.05
    except:
        pass
    
    # Schema type bonus
    important_types = ['PodcastEpisode', 'Article', 'BlogPosting', 'NewsArticle']
    if schema_type in important_types:
        rich_bonus += 0.1
    
    # Has audio bonus
    if metadata.get('has_audio', '') == 'True':
        rich_bonus += 0.15
    
    # Combine all factors
    total_score = similarity_score + recency_bonus + rich_bonus
    
    return total_score


def deduplicate_results(results):
    """Remove duplicates based on URL"""
    if not results or len(results) < 2:
        return results
    
    # Simple deduplication by URL
    seen_urls = set()
    deduped = []
    
    for result in results:
        url = result['metadata'].get('url', '')
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(result)
    
    return deduped


def text_similarity(text1, text2):
    """Calculate text similarity (simple implementation)"""
    if not text1 or not text2:
        return 0
    
    # Simple Jaccard similarity on words
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    
    if not words1 or not words2:
        return 0
    
    intersection = len(words1 & words2)
    union = len(words1 | words2)
    
    return intersection / union if union > 0 else 0


def advanced_deduplicate(results):
    """Advanced deduplication with text similarity check"""
    if not results or len(results) < 2:
        return results
    
    # First, deduplicate by URL
    url_deduped = []
    seen_urls = set()
    for result in results:
        url = result['metadata'].get('url', '')
        if url not in seen_urls:
            seen_urls.add(url)
            url_deduped.append(result)
    
    # Then check for text similarity
    final_results = []
    for i, result_i in enumerate(url_deduped):
        is_duplicate = False
        for result_j in final_results:
            # Compare text similarity
            text_i = result_i['metadata'].get('text', '')
            text_j = result_j['metadata'].get('text', '')
            similarity = text_similarity(text_i, text_j)
            
            # If similarity > 90%, keep the one with higher relevance
            if similarity > 0.9:
                if result_i['relevance'] > result_j['relevance']:
                    final_results.remove(result_j)
                    final_results.append(result_i)
                is_duplicate = True
                break
        
        if not is_duplicate:
            final_results.append(result_i)
    
    return final_results


def autocomplete_query(query):
    """Autocomplete from vector database titles"""
    if not crawler_engine.vector_db:
        return []
    
    try:
        # Get all vectors
        all_results = crawler_engine.vector_db.query(
            query_embeddings=[[0.0] * 384],  # Dummy query to get all
            n_results=50,
            where=None,
            include=["metadatas"]
        )
        
        titles = []
        for metadata in all_results.get('metadatas', []):
            for meta in metadata:
                title = meta.get('title', '')
                if title and title not in titles:
                    titles.append(title)
        
        # Filter by query
        if query:
            filtered = [t for t in titles if query.lower() in t.lower()]
            return filtered[:5]
        
        return titles[:5]
    except Exception as e:
        print(f"⚠️  Chyba při autocomplete: {e}")
        return []


def format_price(price, currency):
    """Format price with currency"""
    if not price:
        return ""
    
    try:
        price_float = float(price)
        if currency == "CZK":
            return f"{price_float:,.2f} Kč"
        else:
            return f"{price_float:,.2f} {currency}"
    except:
        return f"{price} {currency}"


def format_rating(rating, reviews):
    """Format rating with stars"""
    if not rating:
        return ""
    
    try:
        rating_float = float(rating)
        stars = "⭐" * int(round(rating_float))
        empty_stars = "☆" * (5 - int(round(rating_float)))
        return f"{stars}{empty_stars} {rating_float:.1f}/5 ({reviews or 0} recenzí)"
    except:
        return f"⭐ {rating}/5"


HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mini Search - Vyhledávání v3.0</title>
    <style>
        * { 
            box-sizing: border-box; 
        }
        
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; 
            background: #f8f9fa; 
            margin: 0; 
            padding: 0; 
            color: #202124; 
        }
        
        .header { 
            background: white; 
            padding: 25px 5%; 
            border-bottom: 1px solid #ebebeb; 
            display: flex; 
            flex-direction: column; 
            align-items: center; 
            position: sticky; 
            top: 0; 
            z-index: 100; 
            box-shadow: 0 2px 5px rgba(0,0,0,0.03); 
        }
        
        .logo { 
            font-size: 2.2rem; 
            font-weight: 700; 
            margin-bottom: 15px; 
            color: #1a73e8; 
            text-decoration: none; 
            letter-spacing: -1px; 
        }
        
        .search-container { 
            width: 100%; 
            max-width: 650px; 
            position: relative; 
        }
        
        .search-box { 
            width: 100%; 
            display: flex; 
            gap: 10px; 
        }
        
        input[type="text"] { 
            width: 100%; 
            padding: 14px 20px; 
            font-size: 1.05rem; 
            border: 1px solid #dfe1e5; 
            border-radius: 28px; 
            outline: none; 
            box-shadow: 0 1px 6px rgba(32,33,36,0.1); 
            transition: all 0.2s; 
        }
        
        input[type="text"]:focus { 
            border-color: #1a73e8; 
            box-shadow: 0 1px 8px rgba(26,115,232,0.2); 
        }
        
        /* Autocomplete */
        .autocomplete-items { 
            position: absolute; 
            border: 1px solid #dfe1e5; 
            border-top: none; 
            z-index: 99; 
            top: 100%; 
            left: 20px; 
            right: 20px; 
            background: white; 
            border-bottom-left-radius: 12px; 
            border-bottom-right-radius: 12px; 
            box-shadow: 0 4px 10px rgba(0,0,0,0.1); 
            overflow: hidden; 
            animation: fadeIn 0.2s; 
        }
        
        @keyframes fadeIn { 
            from { opacity: 0; } 
            to { opacity: 1; } 
        }
        
        .autocomplete-items div { 
            padding: 10px 15px; 
            cursor: pointer; 
            border-bottom: 1px solid #f1f3f4; 
            font-size: 0.95rem; 
            color: #202124; 
            transition: background 0.2s; 
        }
        
        .autocomplete-items div:hover { 
            background-color: #f1f3f4; 
        }
        
        /* Filters */
        .filters-bar { 
            width: 100%; 
            max-width: 850px; 
            margin: 15px auto 0 auto; 
            display: flex; 
            gap: 10px; 
            padding: 0 15px; 
            flex-wrap: wrap; 
            justify-content: center; 
        }
        
        .filter-btn { 
            background: white; 
            border: 1px solid #dadce0; 
            padding: 6px 14px; 
            border-radius: 16px; 
            font-size: 0.85rem; 
            cursor: pointer; 
            text-decoration: none; 
            color: #5f6368; 
            font-weight: 500; 
            transition: all 0.2s; 
        }
        
        .filter-btn.active, .filter-btn:hover { 
            background: #e8f0fe; 
            color: #1a73e8; 
            border-color: #d2e3fc; 
        }
        
        .filter-btn.active { 
            background: #d2e3fc; 
            border-color: #1a73e8; 
        }
        
        .container { 
            max-width: 850px; 
            margin: 20px auto; 
            padding: 0 15px; 
        }
        
        .stats { 
            color: #70757a; 
            font-size: 0.85rem; 
            margin-bottom: 20px; 
            text-align: center; 
        }
        
        /* Results */
        .result-item { 
            background: white; 
            padding: 22px; 
            margin-bottom: 18px; 
            border-radius: 12px; 
            border: 1px solid #e0e0e0; 
            display: flex; 
            gap: 18px; 
            box-shadow: 0 2px 4px rgba(0,0,0,0.02); 
            transition: all 0.2s; 
        }
        
        .result-item:hover { 
            box-shadow: 0 4px 12px rgba(0,0,0,0.06); 
            transform: translateY(-2px); 
        }
        
        .result-content { 
            flex: 1; 
        }
        
        .badge-row { 
            display: flex; 
            gap: 8px; 
            margin-bottom: 6px; 
            align-items: center; 
            flex-wrap: wrap; 
        }
        
        .badge { 
            background: #e8f0fe; 
            color: #1a73e8; 
            padding: 3px 8px; 
            border-radius: 4px; 
            font-size: 0.75rem; 
            font-weight: 600; 
            text-transform: uppercase; 
        }
        
        .date-badge { 
            color: #70757a; 
            font-size: 0.8rem; 
        }
        
        .result-title { 
            font-size: 1.25rem; 
            margin: 0 0 6px 0; 
            font-weight: 500; 
        }
        
        .result-title a { 
            color: #1a0dab; 
            text-decoration: none; 
        }
        
        .result-title a:hover { 
            text-decoration: underline; 
        }
        
        .result-url { 
            font-size: 0.85rem; 
            color: #006621; 
            margin-bottom: 8px; 
            word-break: break-all; 
        }
        
        .result-snippet { 
            font-size: 0.95rem; 
            color: #4d5156; 
            line-height: 1.6; 
        }
        
        /* Rich Results & Audio */
        .rich-data { 
            margin-top: 12px; 
            padding-top: 10px; 
            border-top: 1px dashed #eee; 
            font-size: 0.85rem; 
            color: #5f6368; 
            display: flex; 
            gap: 15px; 
            flex-wrap: wrap; 
            align-items: center; 
        }
        
        .rating { 
            color: #f4b400; 
            font-weight: bold; 
        }
        
        .price { 
            color: #0f9d58; 
            font-weight: bold; 
        }
        
        .author { 
            color: #5f6368; 
        }
        
        .audio-player { 
            width: 100%; 
            margin-top: 8px; 
            height: 36px; 
        }
        
        .result-img { 
            width: 110px; 
            height: 110px; 
            object-fit: cover; 
            border-radius: 8px; 
            flex-shrink: 0; 
            background: #f1f3f4; 
            transition: all 0.2s; 
        }
        
        .result-img:hover { 
            transform: scale(1.05); 
        }
        
        /* No results */
        .no-results { 
            text-align: center; 
            padding: 60px 20px; 
            color: #70757a; 
        }
        
        .no-results-icon { 
            font-size: 4rem; 
            margin-bottom: 16px; 
        }
        
        /* Loading */
        .loading { 
            text-align: center; 
            padding: 40px; 
            color: #70757a; 
        }
        
        .spinner { 
            display: inline-block; 
            width: 40px; 
            height: 40px; 
            border: 4px solid #f3f3f3; 
            border-top: 4px solid #1a73e8; 
            border-radius: 50%; 
            animation: spin 1s linear infinite; 
        }
        
        @keyframes spin { 
            to { transform: rotate(360deg); } 
        }
        
        /* Backend info */
        .backend-info { 
            font-size: 0.85rem; 
            color: #70757a; 
            margin-top: 10px; 
            text-align: center; 
        }
        
        /* Responsive */
        @media (max-width: 768px) { 
            .result-item { 
                flex-direction: column; 
            }
            
            .result-img { 
                width: 100%; 
                height: 200px; 
            }
            
            .filters-bar { 
                justify-content: center; 
            }
            
            .filter-btn { 
                padding: 8px 12px; 
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <a href="/" class="logo">🔍 Mini Search AI v3.0</a>
        <div class="search-container">
            <form action="/" method="get" class="search-box">
                <input type="text" id="searchInput" name="q" value="{{ query }}" placeholder="Zadejte hledaný výraz, téma nebo otázku..." autocomplete="off" autofocus {{ 'required' if not query else '' }}>
            </form>
            <div id="autocompleteList" class="autocomplete-items"></div>
        </div>
    </div>

    <div class="filters-bar">
        <a href="/?q={{ query }}" class="filter-btn {% if not f and not audio and not price %}active{% endif %}">Vše</a>
        <a href="/?q={{ query }}&f=PodcastEpisode" class="filter-btn {% if f == 'PodcastEpisode' %}active{% endif %}" title="Zobrazit pouze podcasty">🎙️ Podcasty</a>
        <a href="/?q={{ query }}&f=Article,BlogPosting,NewsArticle" class="filter-btn {% if f in ['Article', 'BlogPosting', 'NewsArticle'] %}active{% endif %}" title="Zobrazit pouze články">📰 Články</a>
        <a href="/?q={{ query }}&audio=1" class="filter-btn {% if audio %}active{% endif %}" title="Zobrazit pouze stránky s audio">🎧 S audio</a>
        <a href="/?q={{ query }}&price=1" class="filter-btn {% if price %}active{% endif %}" title="Zobrazit pouze stránky s cenou">💰 S cenou</a>
    </div>

    <div class="backend-info">
        Vector backend: {{ vector_backend|upper }}
    </div>

    <div class="container">
        {% if query %}
            <div class="stats">Nalezeno {{ total }} výsledků (za {{ processing_time }} ms)</div>
            
            {% if results %}
                {% for item in results %}
                <div class="result-item">
                    {% if item.metadata.image %}
                        <img src="{{ item.metadata.image }}" class="result-img" onerror="this.style.display='none'">
                    {% endif %}
                    <div class="result-content">
                        <div class="badge-row">
                            {% if item.metadata.schema_type and item.metadata.schema_type != 'WebPage' %}
                                <span class="badge">{{ item.metadata.schema_type }}</span>
                            {% endif %}
                            {% if item.metadata.published_date %}
                                <span class="date-badge">📅 {{ item.metadata.published_date }}</span>
                            {% endif %}
                        </div>
                        
                        <div class="result-url">{{ item.metadata.url }}</div>
                        <h3 class="result-title"><a href="{{ item.metadata.url }}" target="_blank">{{ item.metadata.title }}</a></h3>
                        <div class="result-snippet">{{ item.metadata.text[:220] }}...</div>
                        
                        {% if item.metadata.has_audio == 'True' and item.metadata.audio_url %}
                            <audio controls class="audio-player">
                                <source src="{{ item.metadata.audio_url }}" type="audio/mpeg">
                                Váš prohlížeč nepodporuje audio element.
                            </audio>
                        {% endif %}

                        {% if item.metadata.schema_details %}
                        <div class="rich-data">
                            {% set schema_details = item.metadata.schema_details|from_json if item.metadata.schema_details else {} %}
                            {% if schema_details.rating %}
                                <span class="rating">{{ format_rating(schema_details.rating, schema_details.reviews or 0) }}</span>
                            {% endif %}
                            {% if schema_details.price %}
                                <span class="price">{{ format_price(schema_details.price, schema_details.currency or 'CZK') }}</span>
                            {% endif %}
                            {% if schema_details.author %}
                                <span class="author">✍️ {{ schema_details.author }}</span>
                            {% endif %}
                        </div>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="no-results">
                    <div class="no-results-icon">🔍</div>
                    <p>Pro výraz <b>"{{ query }}"</b> nebyly nalezeny žádné výsledky.</p>
                    <p style="margin-top: 10px; font-size: 0.9rem; color: #9aa0a6;">
                        Zkuste jiný výraz nebo zkontrolujte, zda byly stránky správně naindexovány.
                    </p>
                </div>
            {% endif %}
        {% else %}
            <div class="no-results">
                <div class="no-results-icon">👋</div>
                <p>Vítejte v Mini Search v3.0!</p>
                <p style="margin-top: 10px; font-size: 0.9rem; color: #9aa0a6;">
                    Vyhledávací index je připraven. Přidejte weby prostřednictvím správcovské konzole.
                </p>
                <p style="margin-top: 20px;">
                    <a href="http://localhost:5000" target="_blank" class="filter-btn" style="padding: 10px 20px;">🔧 Otevřít správcovskou konzoli</a>
                </p>
            </div>
        {% endif %}
    </div>

    <script>
        // Autocomplete functionality
        const input = document.getElementById("searchInput");
        const list = document.getElementById("autocompleteList");
        let autocompleteTimeout;

        input.addEventListener("input", async function() {
            const val = this.value;
            
            // Clear previous timeout
            clearTimeout(autocompleteTimeout);
            
            list.innerHTML = "";
            if (!val || val.length < 2) return;

            // Debounce autocomplete requests
            autocompleteTimeout = setTimeout(async () => {
                try {
                    const res = await fetch(`/autocomplete?q=${encodeURIComponent(val)}`);
                    const data = await res.json();
                    
                    if (data && data.length > 0) {
                        data.forEach(title => {
                            const item = document.createElement("div");
                            const matchIndex = title.toLowerCase().indexOf(val.toLowerCase());
                            if (matchIndex !== -1) {
                                const before = title.substring(0, matchIndex);
                                const match = title.substring(matchIndex, matchIndex + val.length);
                                const after = title.substring(matchIndex + val.length);
                                item.innerHTML = `${before}<strong>${match}</strong>${after}`;
                            } else {
                                item.textContent = title;
                            }
                            item.addEventListener("click", function() {
                                input.value = title;
                                list.innerHTML = "";
                                input.form.submit();
                            });
                            list.appendChild(item);
                        });
                    }
                } catch(e) {
                    console.error("Autocomplete error:", e);
                }
            }, 300); // 300ms debounce
        });

        // Close autocomplete when clicking outside
        document.addEventListener("click", function(e) {
            if (e.target !== input) { 
                list.innerHTML = ""; 
            }
        });

        // Submit form on Enter key
        input.addEventListener("keydown", function(e) {
            if (e.key === "Enter" && list.children.length > 0) {
                // If autocomplete is open, select first item on Enter
                const firstItem = list.children[0];
                if (firstItem) {
                    input.value = firstItem.textContent || firstItem.innerText;
                    list.innerHTML = "";
                    input.form.submit();
                    e.preventDefault();
                }
            }
        });
    </script>
</body>
</html>
'''


@app.route('/')
def search():
    """Main search page"""
    query = request.args.get('q', '').strip()
    f_filter = request.args.get('f', '')
    audio_filter = request.args.get('audio', '')
    price_filter = request.args.get('price', '')
    
    results = []
    total = 0
    processing_time = 0
    
    if query:
        start_time = time.time()
        
        # Build filters
        filters = {}
        if f_filter:
            filter_types = f_filter.split(',')
            filters['schema_type'] = filter_types
        if audio_filter:
            filters['has_audio'] = True
        if price_filter:
            filters['has_price'] = True
        
        # Search vector database
        raw_results = get_vector_results(query, limit=25, filters=filters)
        
        # Deduplicate
        deduped_results = advanced_deduplicate(raw_results)
        
        # Prepare results for template
        results = []
        for result in deduped_results:
            metadata = result['metadata']
            
            results.append({
                'id': result['id'],
                'metadata': metadata,
                'text': metadata.get('text', '')[:500],
                'relevance': result.get('relevance', 0)
            })
        
        total = len(results)
        processing_time = int((time.time() - start_time) * 1000)
    
    return render_template_string(
        HTML_TEMPLATE,
        query=query,
        results=results,
        total=total,
        processing_time=processing_time,
        f=f_filter,
        audio=audio_filter,
        price=price_filter,
        format_price=format_price,
        format_rating=format_rating,
        vector_backend=crawler_engine.VECTOR_BACKEND
    )


@app.route('/autocomplete')
def autocomplete():
    """Autocomplete endpoint"""
    query = request.args.get('q', '').strip()
    
    if not query or len(query) < 2:
        return jsonify([])
    
    try:
        suggestions = autocomplete_query(query)
        return jsonify(suggestions)
    except Exception as e:
        print(f"⚠️  Chyba při autocomplete: {e}")
        return jsonify([])


if __name__ == '__main__':
    print("=" * 70)
    print("Mini Search - Vyhledávací rozhraní v3.0")
    print(f"Vector backend: {crawler_engine.VECTOR_BACKEND}")
    print("=" * 70)
    print(f"Spouštím na http://0.0.0.0:8095")
    print("Ctrl+C pro ukončení")
    print("=" * 70)
    
    try:
        app.run(host='0.0.0.0', port=8095, threaded=True)
    except KeyboardInterrupt:
        print("\n✅ Vyhledávání ukončeno")
