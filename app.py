import os
import re
from difflib import SequenceMatcher
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from flask import Flask, redirect, request, session, url_for, render_template_string, Response, stream_with_context
import json
from dotenv import load_dotenv

# --- FLASK APP AND SESSION SETUP ---
app = Flask(__name__, static_folder='.', static_url_path='/static')
# Load .env file for local development (Vercel will use its own env vars)
load_dotenv()

# This is REQUIRED for sessions to work.
# Vercel: Set this in your Environment Variables.
# Local: Put this in your .env file.
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

# --- SPOTIPY AUTHENTICATION SETUP ---
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
REDIRECT_URI = os.environ.get("REDIRECT_URI") # Should be https://.../callback
SCOPE = "user-library-read playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"

# --- LOGO IMAGE (for use in templates) ---
LOGO_IMG = '<img src="/static/spotify.png" alt="Spotify Filterer" width="40" height="40">'


def get_oauth_manager():
    """Returns a SpotifyOAuth object that uses the user's session for caching."""
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_handler=spotipy.cache_handler.FlaskSessionCacheHandler(session)
    )

def get_spotify_client():
    """Gets a Spotipy client for the current user, or None if not authenticated."""
    oauth_manager = get_oauth_manager()
    token_info = oauth_manager.get_cached_token()

    if not token_info:
        return None
    
    if oauth_manager.is_token_expired(token_info):
        token_info = oauth_manager.refresh_access_token(token_info['refresh_token'])
        session['token_info'] = token_info

    return spotipy.Spotify(auth=token_info['access_token'])


# --- PAGE ROUTES ---

@app.route("/")
def index():
    """
    Homepage.
    Shows login button or the main app (playlist filterer).
    """
    sp = get_spotify_client()
    
    if not sp:
        # User is not logged in
        return render_template_string(HTML_LOGIN_PAGE, logo=LOGO_IMG)

    # User is logged in, show the main app
    user_info = sp.current_user()
    
    # Fetch all user playlists to display in the filter list
    print("Fetching user's playlists...")
    playlists = []
    offset = 0
    limit = 50
    while True:
        results = sp.current_user_playlists(limit=limit, offset=offset)
        if not results['items']:
            break
        
        full_playlist_items = []
        for item in results['items']:
            try:
                pl = sp.playlist(item['id'], fields="id,name,images,tracks.total")
                full_playlist_items.append(pl)
            except Exception:
                pass 
                
        playlists.extend(full_playlist_items)
        offset += limit
    
    print(f"Found {len(playlists)} playlists.")
    
    # Render the main app HTML, passing in user data
    return render_template_string(
        HTML_APP_PAGE, 
        user_name=user_info['display_name'],
        playlists=playlists,
        logo=LOGO_IMG
    )

@app.route("/login")
def login():
    """Redirects user to Spotify to log in."""
    oauth_manager = get_oauth_manager()
    auth_url = oauth_manager.get_authorize_url()
    return redirect(auth_url)

@app.route("/callback")
def callback():
    """
    Handles the redirect from Spotify after login.
    Saves the auth token in the session.
    """
    oauth_manager = get_oauth_manager()
    
    if request.args.get("error"):
        error_msg = request.args.get("error")
        return f"Error from Spotify: {error_msg}"
        
    code = request.args.get("code")
    if not code:
        return "Error: No code provided in callback."

    try:
        token_info = oauth_manager.get_access_token(code)
    except Exception as e:
        return f"Error getting token: {e}"

    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    """Logs the user out by clearing the session."""
    session.clear()
    return redirect(url_for("index"))

@app.route("/run-filter", methods=["POST"])
def run_filter():
    """
    This is the main logic. It runs when the user submits the form.
    Uses streaming response for progress updates.
    """
    sp = get_spotify_client()
    if not sp:
        return "Error: Not authenticated. Please log in again.", 401

    def generate():
        try:
            # Helper to send progress updates
            def send_progress(step, message):
                yield f"data: {json.dumps({'type': 'progress', 'step': step, 'message': message})}\n\n"
            
            # 1. Get data from the submitted form
            form_data = request.form
            target_playlist_link = form_data.get("target_playlist")
            filter_playlist_ids = form_data.getlist("filter_playlists")
            include_liked_songs = form_data.get("include_liked_songs") == "on"
            
            # 2. Get ID from the target playlist link
            target_playlist_id = get_playlist_id_from_link(target_playlist_link)
            if not target_playlist_id:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Invalid Target Playlist link.'})}\n\n"
                return
            
            playlist_name = sp.playlist(target_playlist_id, fields='name')['name']
            
            # Get user's market for availability checking
            user_info = sp.current_user()
            user_market = user_info.get('country', 'US')

            # 3. Fetch target playlist tracks with full details
            yield from send_progress(1, f"Fetching target playlist: '{playlist_name}'...")
            target_tracks = []
            offset = 0
            while True:
                results = sp.playlist_items(
                    target_playlist_id, 
                    limit=100, 
                    offset=offset,
                    fields="items(track(id,name,duration_ms,artists(id,name),external_ids,is_playable,is_local)),next",
                    market=user_market
                )
                if not results['items']:
                    break
                for item in results['items']:
                    track = item.get('track')
                    if track and track.get('id'):
                        target_tracks.append(track)
                offset += 100
            
            # Count how many times each track ID appears in target playlist
            target_id_counts = {}
            for track in target_tracks:
                tid = track['id']
                target_id_counts[tid] = target_id_counts.get(tid, 0) + 1

            # 4. Separate unavailable tracks
            yield from send_progress(2, "Checking for unavailable tracks...")
            unavailable_tracks = []
            available_target_tracks = []
            seen_unavailable_ids = set()
            
            for track in target_tracks:
                is_local = track.get('is_local', False)
                is_playable = track.get('is_playable', True)
                
                if is_local or not is_playable:
                    if track['id'] not in seen_unavailable_ids:
                        unavailable_tracks.append(track)
                        seen_unavailable_ids.add(track['id'])
                else:
                    available_target_tracks.append(track)

            # 5. Build the filter tracks list with full details
            yield from send_progress(3, "Building filter list...")
            all_filter_tracks = []
            all_filter_song_ids = set()

            if include_liked_songs:
                yield from send_progress(3, "Fetching Liked Songs...")
                offset = 0
                while True:
                    results = sp.current_user_saved_tracks(limit=50, offset=offset)
                    if not results['items']:
                        break
                    for item in results['items']:
                        track = item.get('track')
                        if track and track.get('id'):
                            all_filter_tracks.append(track)
                            all_filter_song_ids.add(track['id'])
                    offset += 50
            
            for idx, filter_pid in enumerate(filter_playlist_ids):
                if filter_pid == "liked_songs":
                    continue
                
                filter_playlist_name = sp.playlist(filter_pid, fields='name')['name']
                yield from send_progress(3, f"Fetching playlist {idx+1}/{len(filter_playlist_ids)}: '{filter_playlist_name}'...")
                offset = 0
                while True:
                    results = sp.playlist_items(
                        filter_pid, 
                        limit=100, 
                        offset=offset, 
                        fields="items(track(id,name,duration_ms,artists(id,name),external_ids)),next"
                    )
                    if not results['items']:
                        break
                    for item in results['items']:
                        track = item.get('track')
                        if track and track.get('id'):
                            all_filter_tracks.append(track)
                            all_filter_song_ids.add(track['id'])
                    offset += 100

            # 6. Find exact ID matches - deduplicate by track ID
            yield from send_progress(4, "Finding exact matches...")
            exact_matches = []
            remaining_tracks = []
            seen_exact_ids = set()
            
            for track in available_target_tracks:
                if track['id'] in all_filter_song_ids:
                    if track['id'] not in seen_exact_ids:
                        exact_matches.append({'track': track, 'reason': 'Exact match in filter playlist'})
                        seen_exact_ids.add(track['id'])
                else:
                    remaining_tracks.append(track)

            # 7. Deduplicate remaining_tracks for fuzzy matching
            remaining_unique = []
            seen_remaining_ids = set()
            for track in remaining_tracks:
                if track['id'] not in seen_remaining_ids:
                    remaining_unique.append(track)
                    seen_remaining_ids.add(track['id'])

            # 8. Find fuzzy duplicates between target and filter playlists
            yield from send_progress(5, "Scanning for fuzzy duplicates...")
            fuzzy_duplicates, cross_warnings = find_duplicates_and_warnings(remaining_unique, all_filter_tracks)
            
            fuzzy_dup_ids = {d[0]['id'] for d in fuzzy_duplicates}
            remaining_after_fuzzy = [t for t in remaining_unique if t['id'] not in fuzzy_dup_ids]

            # 9. Find internal duplicates within the target playlist
            yield from send_progress(6, "Scanning for internal duplicates...")
            internal_duplicates = find_internal_duplicates(remaining_after_fuzzy)

            # 10. Compile all tracks to remove (unique IDs only)
            tracks_to_remove_ids = set()
            removal_details = []
            
            for track in unavailable_tracks:
                tracks_to_remove_ids.add(track['id'])
                removal_details.append({
                    'name': track.get('name', 'Unknown'),
                    'artists': ', '.join(a.get('name', '') for a in track.get('artists', [])),
                    'reason': '🚫 Unavailable in your region',
                    'score': 0,
                    'category': 'unavailable'
                })
            
            for match in exact_matches:
                track = match['track']
                tracks_to_remove_ids.add(track['id'])
                removal_details.append({
                    'name': track.get('name', 'Unknown'),
                    'artists': ', '.join(a.get('name', '') for a in track.get('artists', [])),
                    'reason': '✓ Exact match in filter playlist',
                    'score': 100,
                    'category': 'exact'
                })
            
            fuzzy_duplicates_sorted = sorted(fuzzy_duplicates, key=lambda x: x[2], reverse=True)
            for target_track, match_track, score, reasons in fuzzy_duplicates_sorted:
                tracks_to_remove_ids.add(target_track['id'])
                match_name = match_track.get('name', 'Unknown')
                removal_details.append({
                    'name': target_track.get('name', 'Unknown'),
                    'artists': ', '.join(a.get('name', '') for a in target_track.get('artists', [])),
                    'reason': f'🔄 Similar to "{match_name}" ({score}pts: {", ".join(reasons)})',
                    'score': score,
                    'category': 'fuzzy'
                })
            
            internal_duplicates_sorted = sorted(internal_duplicates, key=lambda x: x[2], reverse=True)
            for dup_track, original_track, score, reasons in internal_duplicates_sorted:
                tracks_to_remove_ids.add(dup_track['id'])
                original_name = original_track.get('name', 'Unknown')
                removal_details.append({
                    'name': dup_track.get('name', 'Unknown'),
                    'artists': ', '.join(a.get('name', '') for a in dup_track.get('artists', [])),
                    'reason': f'📋 Duplicate of "{original_name}" in playlist ({score}pts: {", ".join(reasons)})',
                    'score': score,
                    'category': 'internal'
                })

            # 11. Remove the songs in batches
            actual_removals = 0
            if tracks_to_remove_ids:
                yield from send_progress(7, f"Removing {len(tracks_to_remove_ids)} tracks...")
                tracks_list = list(tracks_to_remove_ids)
                
                for i in range(0, len(tracks_list), 100):
                    batch = tracks_list[i:i+100]
                    sp.playlist_remove_all_occurrences_of_items(target_playlist_id, batch)
                    for tid in batch:
                        actual_removals += target_id_counts.get(tid, 1)
                    yield from send_progress(7, f"Removed {min(i+100, len(tracks_list))}/{len(tracks_list)} tracks...")

            # 12. Build HTML response
            html_parts = []
            
            if actual_removals > 0:
                html_parts.append(f"<div style='color: #1DB954; font-size: 1.2rem; margin-bottom: 1rem;'>✅ Removed {actual_removals} songs from '{escape_html(playlist_name)}'</div>")
                if actual_removals != len(tracks_to_remove_ids):
                    html_parts.append(f"<div style='color: #aaa; font-size: 0.9rem; margin-bottom: 1rem;'>({len(tracks_to_remove_ids)} unique tracks, {actual_removals - len(tracks_to_remove_ids)} were duplicates in playlist)</div>")
            else:
                html_parts.append(f"<div style='color: #1DB954;'>✅ No songs to remove from '{escape_html(playlist_name)}'</div>")
            
            if unavailable_tracks:
                html_parts.append(f"<div style='margin: 0.5rem 0;'>🚫 {len(unavailable_tracks)} unavailable</div>")
            if exact_matches:
                html_parts.append(f"<div style='margin: 0.5rem 0;'>✓ {len(exact_matches)} exact matches</div>")
            if fuzzy_duplicates:
                html_parts.append(f"<div style='margin: 0.5rem 0;'>🔄 {len(fuzzy_duplicates)} fuzzy duplicates</div>")
            if internal_duplicates:
                html_parts.append(f"<div style='margin: 0.5rem 0;'>📋 {len(internal_duplicates)} internal duplicates</div>")
            
            category_order = {'exact': 0, 'fuzzy': 1, 'internal': 2, 'unavailable': 3}
            removal_details_sorted = sorted(
                removal_details, 
                key=lambda x: (-x['score'], category_order.get(x['category'], 99))
            )
            
            if removal_details_sorted:
                html_parts.append("<h4 style='margin-top: 1.5rem;'>Removed Songs:</h4>")
                html_parts.append("<ul class='removed-song-list'>")
                for detail in removal_details_sorted:
                    name = escape_html(detail['name'])
                    artists = escape_html(detail['artists'])
                    reason = escape_html(detail['reason'])
                    html_parts.append(f"<li><strong>{name}</strong> - {artists}<br><small style='color: #aaa;'>{reason}</small></li>")
                html_parts.append("</ul>")
            
            cross_warnings_sorted = sorted(cross_warnings, key=lambda x: x[2], reverse=True)
            if cross_warnings_sorted:
                html_parts.append("<h4 style='margin-top: 1.5rem; color: #FFA500;'>⚠️ Potential Duplicates (not removed):</h4>")
                html_parts.append("<p style='color: #aaa; font-size: 0.9rem;'>These songs are similar but didn't meet the threshold for automatic removal.</p>")
                html_parts.append("<ul class='removed-song-list' style='border-left: 3px solid #FFA500;'>")
                for target_track, similar_track, score, reasons in cross_warnings_sorted[:20]:
                    t_name = escape_html(target_track.get('name', 'Unknown'))
                    t_artists = escape_html(', '.join(a.get('name', '') for a in target_track.get('artists', [])))
                    s_name = escape_html(similar_track.get('name', 'Unknown'))
                    html_parts.append(f"<li><strong>{t_name}</strong> - {t_artists}<br><small style='color: #FFA500;'>Similar to \"{s_name}\" ({score}pts: {', '.join(reasons)})</small></li>")
                if len(cross_warnings_sorted) > 20:
                    html_parts.append(f"<li style='color: #aaa;'>...and {len(cross_warnings_sorted) - 20} more warnings</li>")
                html_parts.append("</ul>")
            
            yield f"data: {json.dumps({'type': 'complete', 'html': ''.join(html_parts)})}\n\n"

        except Exception as e:
            print(f"An error occurred: {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


def escape_html(text):
    """Escape HTML special characters."""
    if not text:
        return ""
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


# --- HELPER FUNCTIONS (from our old script) ---

def get_playlist_id_from_link(link):
    """Extracts the Playlist ID from a Spotify URL or URI."""
    if not link: return None
    if "open.spotify.com/playlist/" in link:
        return link.split("playlist/")[1].split("?")[0]
    elif "spotify:playlist:" in link:
        return link.split("spotify:playlist:")[1]
    else:
        return None


# --- DUPLICATE DETECTION HELPERS ---def normalize_title(title):
    """Normalize a track title for comparison by removing version indicators."""
    if not title:
        return ""
    title = title.lower()
    # Remove common suffixes that indicate versions
    patterns = [
        r'\s*[-–—]\s*remaster(ed)?\s*\d*',
        r'\s*[-–—]\s*\d+\s*remaster',
        r'\s*\(remaster(ed)?\s*\d*\)',
        r'\s*\(deluxe.*?\)',
        r'\s*\(expanded.*?\)',
        r'\s*\(anniversary.*?\)',
        r'\s*\(bonus track.*?\)',
        r'\s*\(album version.*?\)',
        r'\s*\(original.*?\)',
        r'\s*\(single version.*?\)',
        r'\s*\(radio edit.*?\)',
        r'\s*\(explicit.*?\)',
        r'\s*\(clean.*?\)',
        r'\s*[-–—]\s*live.*$',
        r'\s*\(live.*?\)',
        r'\s*\(acoustic.*?\)',
        r'\s*[-–—]\s*from\s+".*"',
        r'\s*\(from\s+".*"\)',
        r'\s*\(from\s+.*?\)',
        r'\s*[-–—]\s*mono.*$',
        r'\s*\(mono.*?\)',
        r'\s*[-–—]\s*stereo.*$',
        r'\s*\(stereo.*?\)',
    ]
    for pattern in patterns:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE)
    
    # Remove featuring artists from title
    title = re.sub(r'\s*(feat\.?|ft\.?|featuring)\s+.*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*\((feat\.?|ft\.?|featuring).*?\)', '', title, flags=re.IGNORECASE)
    
    # Remove extra whitespace
    title = ' '.join(title.split())
    return title.strip()


def fuzzy_title_match(title1, title2):
    """Returns similarity ratio between two titles (0.0 to 1.0)."""
    return SequenceMatcher(None, title1, title2).ratio()


def duration_within_threshold(duration1, duration2):
    """Check if two durations are within acceptable threshold."""
    if not duration1 or not duration2:
        return False
    # Use max of 10 seconds or 3% of the longer song
    max_duration = max(duration1, duration2)
    threshold = max(10000, max_duration * 0.03)  # in milliseconds
    return abs(duration1 - duration2) <= threshold


def artists_overlap(artists1, artists2):
    """Check if there's any artist overlap between two tracks."""
    if not artists1 or not artists2:
        return False
    ids1 = {a['id'] for a in artists1 if a.get('id')}
    ids2 = {a['id'] for a in artists2 if a.get('id')}
    return len(ids1 & ids2) > 0


def artists_exact_match(artists1, artists2):
    """Check if artist lists match exactly."""
    if not artists1 or not artists2:
        return False
    ids1 = {a['id'] for a in artists1 if a.get('id')}
    ids2 = {a['id'] for a in artists2 if a.get('id')}
    return ids1 == ids2 and len(ids1) > 0


def get_isrc(track):
    """Extract ISRC from track if available."""
    external_ids = track.get('external_ids', {})
    return external_ids.get('isrc')


def calculate_similarity_score(track1, track2):
    """
    Calculate similarity score between two tracks.
    Returns (score, reasons) where score >= 70 means duplicate, 40-69 means warning.
    """
    score = 0
    reasons = []
    
    # Check ISRC first (instant match)
    isrc1 = get_isrc(track1)
    isrc2 = get_isrc(track2)
    if isrc1 and isrc2 and isrc1 == isrc2:
        return (100, ["Same ISRC (identical recording)"])
    
    # Title comparison (either/or, not cumulative)
    norm_title1 = normalize_title(track1.get('name', ''))
    norm_title2 = normalize_title(track2.get('name', ''))
    
    title_score = 0
    if norm_title1 and norm_title2:
        if norm_title1 == norm_title2:
            title_score = 40
            reasons.append("Exact title match")
        else:
            similarity = fuzzy_title_match(norm_title1, norm_title2)
            if similarity >= 0.9:
                title_score = 25
                reasons.append(f"Similar title ({similarity:.0%})")
    score += title_score
    
    # Duration comparison
    dur1 = track1.get('duration_ms')
    dur2 = track2.get('duration_ms')
    if duration_within_threshold(dur1, dur2):
        score += 30
        diff_sec = abs(dur1 - dur2) / 1000 if dur1 and dur2 else 0
        reasons.append(f"Similar duration (±{diff_sec:.1f}s)")
    
    # Artist comparison
    artists1 = track1.get('artists', [])
    artists2 = track2.get('artists', [])
    if artists_overlap(artists1, artists2):
        score += 30
        reasons.append("Shared artist(s)")
        if artists_exact_match(artists1, artists2):
            score += 10
            reasons[-1] = "Same artist(s)"
    
    return (score, reasons)


def find_duplicates_and_warnings(target_tracks, filter_tracks):
    """
    Find duplicates and potential duplicates between target and filter playlists.
    
    Returns:
        duplicates: list of (target_track, matching_track, score, reasons)
        warnings: list of (target_track, similar_track, score, reasons)
    """
    duplicates = []
    warnings = []
    
    # Build index by normalized title for faster lookup
    filter_by_title = {}
    for track in filter_tracks:
        norm_title = normalize_title(track.get('name', ''))
        if norm_title:
            if norm_title not in filter_by_title:
                filter_by_title[norm_title] = []
            filter_by_title[norm_title].append(track)
    
    # Also index by ISRC for instant matches
    filter_by_isrc = {}
    for track in filter_tracks:
        isrc = get_isrc(track)
        if isrc:
            filter_by_isrc[isrc] = track
    
    seen_target_ids = set()  # Track which target songs we've already matched
    
    for target_track in target_tracks:
        if not target_track or not target_track.get('id'):
            continue
        if target_track['id'] in seen_target_ids:
            continue
            
        target_isrc = get_isrc(target_track)
        target_norm_title = normalize_title(target_track.get('name', ''))
        
        best_match = None
        best_score = 0
        best_reasons = []
        
        # Check ISRC first
        if target_isrc and target_isrc in filter_by_isrc:
            match_track = filter_by_isrc[target_isrc]
            best_match = match_track
            best_score = 100
            best_reasons = ["Same ISRC (identical recording)"]
        else:
            # Check tracks with similar titles
            candidates = []
            
            # Exact normalized title matches
            if target_norm_title in filter_by_title:
                candidates.extend(filter_by_title[target_norm_title])
            
            # Also check fuzzy matches (this is slower but catches more)
            for norm_title, tracks in filter_by_title.items():
                if norm_title != target_norm_title:
                    similarity = fuzzy_title_match(target_norm_title, norm_title)
                    if similarity >= 0.85:  # Lower threshold for candidate selection
                        candidates.extend(tracks)
            
            # Score each candidate
            for candidate in candidates:
                if candidate.get('id') == target_track.get('id'):
                    continue  # Skip exact same track
                score, reasons = calculate_similarity_score(target_track, candidate)
                if score > best_score:
                    best_score = score
                    best_match = candidate
                    best_reasons = reasons
        
        if best_match:
            if best_score >= 70:
                duplicates.append((target_track, best_match, best_score, best_reasons))
                seen_target_ids.add(target_track['id'])
            elif best_score >= 40:
                warnings.append((target_track, best_match, best_score, best_reasons))
    
    return duplicates, warnings


def find_internal_duplicates(tracks):
    """
    Find duplicates within a single playlist.
    Returns list of (track_to_remove, original_track, score, reasons)
    """
    duplicates = []
    dominated_ids = set()  # Tracks that are duplicates of something else
    
    # Build index
    by_title = {}
    by_isrc = {}
    for track in tracks:
        if not track or not track.get('id'):
            continue
        norm_title = normalize_title(track.get('name', ''))
        if norm_title:
            if norm_title not in by_title:
                by_title[norm_title] = []
            by_title[norm_title].append(track)
        isrc = get_isrc(track)
        if isrc:
            if isrc not in by_isrc:
                by_isrc[isrc] = []
            by_isrc[isrc].append(track)
    
    # Check ISRC duplicates first
    for isrc, isrc_tracks in by_isrc.items():
        if len(isrc_tracks) > 1:
            # Keep the first one, mark others as duplicates
            original = isrc_tracks[0]
            for dup in isrc_tracks[1:]:
                if dup['id'] not in dominated_ids:
                    duplicates.append((dup, original, 100, ["Same ISRC (identical recording)"]))
                    dominated_ids.add(dup['id'])
    
    # Check title-based duplicates
    for norm_title, title_tracks in by_title.items():
        if len(title_tracks) <= 1:
            continue
        
        # Compare each pair
        for i, track1 in enumerate(title_tracks):
            if track1['id'] in dominated_ids:
                continue
            for track2 in title_tracks[i+1:]:
                if track2['id'] in dominated_ids:
                    continue
                if track1['id'] == track2['id']:
                    continue
                    
                score, reasons = calculate_similarity_score(track1, track2)
                if score >= 70:
                    # Keep track1, remove track2
                    duplicates.append((track2, track1, score, reasons))
                    dominated_ids.add(track2['id'])
    
    return duplicates

# --- HTML TEMPLATES ---
# We are embedding the HTML directly in our Python file for simplicity.

HTML_LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Spotify Filterer</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; 
            min-height: 100vh; 
            background-color: #121212; 
            color: #fff;
            
            /* The animated background */
            background: linear-gradient(-45deg, #121212, #191919, #0d2a14, #191919);
            background-size: 400% 400%;
            animation: gradientBG 25s ease infinite;
            
            /* Center everything */
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }
        
        @keyframes gradientBG {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }

        .login-wrapper {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
            padding: 2rem;
        }

        .header {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 1rem;
            margin-bottom: 2.5rem;
        }
        .header img {
            width: 48px;
            height: 48px;
        }
        .header h1 {
            font-size: 2.5rem;
            font-weight: 700;
        }

        .container { 
            background: #282828; 
            padding: 3rem; 
            border-radius: 1rem; 
            width: 100%;
            max-width: 400px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }
        .container p {
            font-size: 1.1rem;
            color: #b3b3b3;
            margin-bottom: 2rem;
        }
        .login-btn { 
            background-color: #1DB954; 
            color: white; 
            padding: 1rem 2rem;
            border: none; 
            border-radius: 500px; 
            text-decoration: none; 
            font-size: 1.1rem; 
            font-weight: 700; 
            cursor: pointer; 
            display: block;
            width: 100%;
            transition: background-color 0.2s, transform 0.1s;
        }
        .login-btn:hover { 
            background-color: #1ED760; 
            transform: scale(1.02);
        }
        .login-btn:active {
            transform: scale(0.98);
        }
    </style>
</head>
<body>
    <div class="login-wrapper">
        <div class="header">
            {{ logo|safe }}
            <h1>Spotify Filterer</h1>
        </div>
        <div class="container">
            <p>Log in to get started.</p>
            <a href="{{ url_for('login') }}" class="login-btn">Login with Spotify</a>
        </div>
    </div>
</body>
</html>
"""

HTML_APP_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Spotify Filterer</title>
    <style>
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; 
            background-color: #121212; 
            color: #fff; 
            margin: 0; 
            padding: 2rem;
            
            /* The new animated background */
            background: linear-gradient(-45deg, #121212, #191919, #0d2a14, #191919);
            background-size: 400% 400%;
            animation: gradientBG 25s ease infinite;
        }

        @keyframes gradientBG {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }

        .header { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            border-bottom: 1px solid #282828; 
            padding-bottom: 2rem; 
            margin-bottom: 2rem;
        }
        .header .title {
            display: flex;
            align-items: center;
            gap: 1rem;
        }
        .header h1 { margin: 0; }
        .header span { font-size: 0.9rem; }
        .logout-btn { background: #333; color: white; text-decoration: none; padding: 0.5rem 1rem; border-radius: 500px; font-size: 0.9rem; font-weight: bold; }
        .logout-btn:hover { background: #555; }
        
        .content { 
            display: grid; 
            grid-template-columns: 1fr; /* Single column on mobile */
            gap: 2rem; 
            max-width: 1400px; /* Wider max width */
            margin-left: auto; 
            margin-right: auto;
            align-items: center; /* Vertically center the cards */
        }
        /* Asymmetrical layout on larger screens */
        @media (min-width: 900px) { 
            .content { grid-template-columns: 1fr 3fr; } /* 1:3 ratio */
        }
        
        .box { background: #181818; padding: 1.5rem; border-radius: 1rem; }
        .box h2 { 
            margin-top: 0; 
            border-bottom: 1px solid #282828; 
            padding-bottom: 0.5rem; 
        }
        
        /* Left Card ("Target") Specific Styles */
        .target-card { padding: 2rem; } /* More padding */
        .target-card h2 { font-size: 1.8rem; } /* Bigger text */
        .target-card p { font-size: 1.1rem; }
        .target-card .form-group label { font-size: 1rem; }

        .form-group { margin-bottom: 1.5rem; }
        .form-group label { display: block; margin-bottom: 0.5rem; font-weight: bold; }
        .form-group input[type='text'] { width: 100%; padding: 1rem; background: #282828; border: 1px solid #555; border-radius: 0.5rem; color: #fff; box-sizing: border-box; font-size: 1rem; }
        
        /* Right Card ("Filter") Specific Styles */
        .filter-card { padding: 2rem; }
        .filter-card h2 { font-size: 1.8rem; }
        .filter-card p { font-size: 1.1rem; }

        .playlist-list { 
            max-height: 700px; /* Taller list */
            overflow-y: auto; 
            background: #282828; 
            border-radius: 0.5rem; 
            padding: 1rem; 
            border: 1px solid #555;
            /* This creates the multi-column grid */
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 0.75rem; /* Space between items */
        }
        
        /* New Playlist Item Styling */
        .playlist-item {
            display: flex;
            align-items: center;
            padding: 0.75rem; /* More padding */
            border-radius: 8px;
            transition: background-color 0.2s;
            cursor: pointer;
            background-color: #181818; /* Darker item background */
            overflow: hidden; /* Ensure no overflow */
        }
        .playlist-item:hover {
            background-color: #3a3a3a;
        }
        
        .playlist-item input[type='checkbox'] {
            accent-color: #1DB954; /* Style the checkbox */
            width: 1.3rem; /* Larger checkbox */
            height: 1.3rem;
            flex-shrink: 0; 
        }
        
        .playlist-cover {
            width: 50px;
            height: 50px;
            object-fit: cover;
            border-radius: 4px; /* Spotify-like rounded square */
            margin-left: 1rem;
            margin-right: 1rem;
            flex-shrink: 0;
            background: #333; /* Placeholder background */
        }
        .playlist-cover.placeholder {
            display: grid;
            place-items: center;
            font-size: 1.5rem;
        }
        
        .playlist-info {
            display: flex;
            flex-direction: column;
            overflow: hidden; /* Prevent long names from breaking layout */
        }
        .playlist-name {
            font-size: 1rem; /* Larger name */
            font-weight: bold;
            color: #fff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .playlist-count {
            font-size: 0.9rem;
            color: #aaa;
        }
        
        .submit-btn { 
            width: 100%; 
            background-color: #1DB954; 
            color: white; 
            padding: 1.25rem 2rem; /* Taller button */
            border: none; 
            border-radius: 500px; 
            text-decoration: none; 
            font-size: 1.4rem; /* Bigger button text */
            font-weight: bold; 
            cursor: pointer; 
            margin-top: 1rem; 
        }
        .submit-btn:hover { background-color: #1ED760; }
        
        #response-box { 
            margin-top: 1.5rem; 
            background: #282828; 
            padding: 1.5rem; 
            border-radius: 0.5rem; 
            display: none; 
            font-size: 1.1rem;
        }
        
        /* Style for the new removed songs list */
        .removed-song-list {
            max-height: 300px;
            overflow-y: auto;
            background: #121212;
            padding: 1rem;
            border-radius: 8px;
            font-size: 0.9rem;
            list-style-type: none;
            margin-bottom: 0;
        }
        .removed-song-list li {
            padding: 0.5rem 0;
            border-bottom: 1px solid #282828;
        }
        .removed-song-list li:last-child {
            border-bottom: none;
        }
        
        /* Progress bar styles */
        .progress-container {
            padding: 1rem 0;
        }
        .progress-bar {
            width: 100%;
            height: 8px;
            background: #333;
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 1rem;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #1DB954, #1ED760);
            border-radius: 4px;
            width: 0%;
            transition: width 0.3s ease;
        }
        .progress-text {
            color: #b3b3b3;
            font-size: 0.95rem;
        }

    </style>
</head>
<body>
    <div class="header">
        <div class="title">
            {{ logo|safe }}
            <h1>Spotify Filterer</h1>
        </div>
        <span>Logged in as: <b>{{ user_name }}</b> <a href="{{ url_for('logout') }}" class="logout-btn">Logout</a></span>
    </div>

    <!-- Form now wraps both columns -->
    <form id="filter-form">
    <div class="content">
        <div class="box target-card">
            <h2>1. Target Playlist</h2>
            <p>Paste the link of the playlist you want to clean up.</p>
            <div class="form-group">
                <label for="target_playlist">Target Playlist Link</label>
                <input type="text" id="target_playlist" name="target_playlist" required placeholder="https://open.spotify.com/playlist/...">
            </div>
            
            <h2>3. Run Filter</h2>
            <p>This will permanently remove songs from your target playlist.</p>
            <button type="submit" class="submit-btn">Start Filtering</button>
        </div>

        <div class="box filter-card">
            <h2>2. Filter Playlists</h2>
            <p>Select which songs to remove. Any song from these sources will be removed from your target playlist.</p>
            <div class="playlist-list" id="filter-playlists-container">
                
                <!-- Styled Liked Songs Item -->
                <label class="playlist-item">
                    <input type="checkbox" name="include_liked_songs" checked>
                    <div class="playlist-cover placeholder">
                        <!-- Inline SVG for a white heart -->
                        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="white">
                            <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/>
                        </svg>
                    </div>
                    <div class="playlist-info">
                        <span class="playlist-name">Your Liked Songs</span>
                    </div>
                </label>
                
                <!-- Playlists will be populated here -->
                {% for playlist in playlists %}
                <label class="playlist-item">
                    <input type="checkbox" name="filter_playlists" value="{{ playlist.id }}">
                    
                    {% if playlist.images and playlist.images|length > 0 %}
                        <img src="{{ playlist.images[-1].url }}" alt="{{ playlist.name }} cover" class="playlist-cover">
                    {% else %}
                        <!-- Placeholder for playlists with no image -->
                        <div class="playlist-cover placeholder">
                            <span>🎵</span>
                        </div>
                    {% endif %}

                    <div class="playlist-info">
                        <span class="playlist-name">{{ playlist.name }}</span>
                        <span class="playlist-count">{{ playlist.tracks.total }} songs</span>
                    </div>
                </label>
                {% endfor %}
            </div>
        </div>
    </div>
    </form> <!-- Form tag closes here -->
    
    <div style="max-width: 1400px; margin-left: auto; margin-right: auto;">
        <div id="response-box"></div>
    </div>

    <script>
        document.getElementById('filter-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const form = e.target;
            const formData = new FormData(form);
            const submitBtn = form.querySelector('.submit-btn');
            const responseBox = document.getElementById('response-box');
            
            submitBtn.disabled = true;
            submitBtn.textContent = 'Filtering...';
            responseBox.style.display = 'block';
            responseBox.style.color = '#fff';
            
            // Progress steps
            const steps = [
                'Initializing...',
                'Fetching target playlist...',
                'Checking availability...',
                'Building filter list...',
                'Finding exact matches...',
                'Scanning for fuzzy duplicates...',
                'Scanning for internal duplicates...',
                'Removing tracks...'
            ];
            
            // Show progress UI
            responseBox.innerHTML = `
                <div class="progress-container">
                    <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
                    <div class="progress-text" id="progress-text">Starting...</div>
                </div>
            `;
            
            try {
                const response = await fetch("{{ url_for('run_filter') }}", {
                    method: 'POST',
                    body: formData
                });
                
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n\n');
                    buffer = lines.pop() || '';
                    
                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            try {
                                const data = JSON.parse(line.slice(6));
                                
                                if (data.type === 'progress') {
                                    const progressFill = document.getElementById('progress-fill');
                                    const progressText = document.getElementById('progress-text');
                                    const percent = (data.step / 7) * 100;
                                    progressFill.style.width = percent + '%';
                                    progressText.textContent = data.message;
                                } else if (data.type === 'complete') {
                                    responseBox.style.color = '#1DB954';
                                    responseBox.innerHTML = data.html;
                                } else if (data.type === 'error') {
                                    responseBox.style.color = '#FF4500';
                                    responseBox.innerHTML = 'Error: ' + data.message;
                                }
                            } catch (parseErr) {
                                console.error('Parse error:', parseErr);
                            }
                        }
                    }
                }
                
            } catch (error) {
                responseBox.style.color = '#FF4500';
                responseBox.innerHTML = 'A network error occurred: ' + error.message;
            } finally {
                submitBtn.disabled = false;
                submitBtn.textContent = 'Start Filtering';
            }
        });
    </script>
</body>
</html>
"""

# This makes the app runnable locally for testing (python app.py)
# Vercel will use a different method to run the 'app' object
if __name__ == "__main__":
    app.run(debug=True, port=8080)