import os
import re
from difflib import SequenceMatcher
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from flask import Flask, redirect, request, session, url_for, render_template_string
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
    Main filtering logic. Processes the form and redirects to results page.
    """
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("index"))

    try:
        # 1. Get data from the submitted form
        form_data = request.form
        target_playlist_link = form_data.get("target_playlist")
        filter_playlist_ids = form_data.getlist("filter_playlists")
        include_liked_songs = form_data.get("include_liked_songs") == "on"
        
        # 2. Get ID from the target playlist link
        target_playlist_id = get_playlist_id_from_link(target_playlist_link)
        if not target_playlist_id:
            return render_template_string(HTML_RESULTS_PAGE, 
                logo=LOGO_IMG,
                error="Invalid Target Playlist link.",
                playlist_name="Unknown",
                results=None
            )
        
        playlist_name = sp.playlist(target_playlist_id, fields='name')['name']

        # 3. Fetch target playlist tracks with full details
        target_tracks = []
        seven_rings_in_fetch = []  # Debug
        offset = 0
        while True:
            results = sp.playlist_items(
                target_playlist_id, 
                limit=100, 
                offset=offset,
                fields="items(track(id,name,duration_ms,artists(id,name),external_ids,is_playable,is_local)),next"
            )
            if not results['items']:
                break
            for item in results['items']:
                track = item.get('track')
                if track and track.get('id'):
                    target_tracks.append(track)
                    # Debug: track all 7 rings during fetch
                    if track.get('name') and '7 rings' in track.get('name', '').lower():
                        seven_rings_in_fetch.append(f"Fetched at offset {offset}: '{track['name']}' ID={track['id']}")
            offset += 100
        
        # Count how many times each track ID appears in target playlist
        target_id_counts = {}
        for track in target_tracks:
            tid = track['id']
            target_id_counts[tid] = target_id_counts.get(tid, 0) + 1

        # 4. Check track availability (need market parameter for this)
        # Get user's market
        user_info = sp.current_user()
        user_market = user_info.get('country', 'US')
        
        # Build a set of unavailable track IDs by checking with market parameter
        unavailable_ids = set()
        offset = 0
        while True:
            results = sp.playlist_items(
                target_playlist_id, 
                limit=100, 
                offset=offset,
                fields="items(track(id,is_playable,is_local)),next",
                market=user_market
            )
            if not results['items']:
                break
            for item in results['items']:
                track = item.get('track')
                if track and track.get('id'):
                    is_local = track.get('is_local', False)
                    is_playable = track.get('is_playable', True)
                    if is_local or not is_playable:
                        unavailable_ids.add(track['id'])
            offset += 100
        
        # Separate tracks based on availability
        unavailable_tracks = []
        available_target_tracks = []
        seen_unavailable_ids = set()
        
        for track in target_tracks:
            if track['id'] in unavailable_ids:
                if track['id'] not in seen_unavailable_ids:
                    unavailable_tracks.append(track)
                    seen_unavailable_ids.add(track['id'])
            else:
                available_target_tracks.append(track)

        # 5. Build the filter tracks list with full details
        all_filter_tracks = []
        all_filter_song_ids = set()

        if include_liked_songs:
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
        
        for filter_pid in filter_playlist_ids:
            if filter_pid == "liked_songs":
                continue
            
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

        # 6. Find exact ID matches - but keep ALL tracks for fuzzy matching
        exact_matches = []
        exact_match_ids = set()
        
        for track in available_target_tracks:
            if track['id'] in all_filter_song_ids:
                if track['id'] not in exact_match_ids:
                    exact_matches.append({'track': track, 'reason': 'Exact match in filter playlist'})
                    exact_match_ids.add(track['id'])

        # 7. Get unique tracks from target for fuzzy matching (ALL of them, not just non-exact)
        target_unique = []
        seen_target_ids = set()
        for track in available_target_tracks:
            if track['id'] not in seen_target_ids:
                target_unique.append(track)
                seen_target_ids.add(track['id'])

        # 8. Find fuzzy duplicates between target and filter playlists
        # This will catch different versions of the same song (different IDs but same title/artist)
        # Exclude tracks that already had exact matches
        tracks_for_fuzzy = [t for t in target_unique if t['id'] not in exact_match_ids]
        fuzzy_duplicates, cross_warnings = find_duplicates_and_warnings(tracks_for_fuzzy, all_filter_tracks)
        
        fuzzy_dup_ids = {d[0]['id'] for d in fuzzy_duplicates}
        remaining_after_fuzzy = [t for t in tracks_for_fuzzy if t['id'] not in fuzzy_dup_ids]

        # 9. Find internal duplicates within the target playlist
        internal_duplicates = find_internal_duplicates(remaining_after_fuzzy)

        # 10. Compile all tracks to remove (unique IDs only)
        tracks_to_remove_ids = set()
        removal_details = []
        
        # Add unavailable tracks
        for track in unavailable_tracks:
            tracks_to_remove_ids.add(track['id'])
            removal_details.append({
                'name': track.get('name', 'Unknown'),
                'artists': ', '.join(a.get('name', '') for a in track.get('artists', [])),
                'reason': '🚫 Unavailable/Local file',
                'score': 0,
                'category': 'unavailable'
            })
        
        for match in exact_matches:
            track = match['track']
            tracks_to_remove_ids.add(track['id'])
            removal_details.append({
                'name': track.get('name', 'Unknown'),
                'artists': ', '.join(a.get('name', '') for a in track.get('artists', [])),
                'reason': f"✓ Exact match in filter playlist [ID: {track['id'][:8]}...]",
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
        failed_removals = []
        removal_log = []  # Debug log
        
        # Debug: Find 7 rings specifically
        seven_rings_debug = []
        
        # Show what we found during initial fetch
        seven_rings_debug.append(f"=== INITIAL FETCH: Found {len(seven_rings_in_fetch)} '7 rings' tracks ===")
        seven_rings_debug.extend(seven_rings_in_fetch)
        seven_rings_debug.append(f"=== Total tracks fetched: {len(target_tracks)} ===")
        
        # Check what 7 rings versions exist in target
        for track in target_tracks:
            if '7 rings' in track.get('name', '').lower():
                tid = track['id']
                in_exact = tid in exact_match_ids
                in_fuzzy = tid in fuzzy_dup_ids
                in_removal = tid in tracks_to_remove_ids
                seven_rings_debug.append(f"Target: '{track['name']}' ID={tid}, exact={in_exact}, fuzzy={in_fuzzy}, will_remove={in_removal}")
        
        # Check what 7 rings versions exist in filter
        for track in all_filter_tracks:
            if '7 rings' in track.get('name', '').lower():
                seven_rings_debug.append(f"Filter: '{track['name']}' ID={track['id']}, duration={track.get('duration_ms')}ms")
        
        # Check if the non-exact 7 rings was in fuzzy matching input
        for track in tracks_for_fuzzy:
            if '7 rings' in track.get('name', '').lower():
                seven_rings_debug.append(f"Fuzzy input: '{track['name']}' ID={track['id']}, duration={track.get('duration_ms')}ms")
        
        if tracks_to_remove_ids:
            tracks_list = list(tracks_to_remove_ids)
            
            for i in range(0, len(tracks_list), 100):
                batch = tracks_list[i:i+100]
                batch_uris = [f"spotify:track:{tid}" for tid in batch]
                try:
                    result = sp.playlist_remove_all_occurrences_of_items(target_playlist_id, batch_uris)
                    removal_log.append(f"Batch {i//100 + 1}: Sent {len(batch)} tracks, API response: {result}")
                    for tid in batch:
                        actual_removals += target_id_counts.get(tid, 1)
                except Exception as e:
                    removal_log.append(f"Batch {i//100 + 1}: FAILED - {str(e)}")
                    failed_removals.extend(batch)
        
        # After removal, verify a sample of tracks are actually gone
        verification_results = []
        sample_tracks = list(tracks_to_remove_ids)[:5]  # Check first 5
        
        # Re-fetch the playlist to verify
        post_removal_ids = set()
        offset = 0
        while True:
            results = sp.playlist_items(target_playlist_id, limit=100, offset=offset, fields="items(track(id,name)),next")
            if not results['items']:
                break
            for item in results['items']:
                track = item.get('track')
                if track and track.get('id'):
                    post_removal_ids.add(track['id'])
                    # Check for 7 rings after removal
                    if '7 rings' in track.get('name', '').lower():
                        seven_rings_debug.append(f"AFTER REMOVAL: '7 rings' still exists with ID={track['id']}")
            offset += 100
        
        for tid in sample_tracks:
            still_exists = tid in post_removal_ids
            track_name = next((d['name'] for d in removal_details if tid in d.get('reason', '')), 'Unknown')
            verification_results.append({
                'id': tid[:12],
                'still_in_playlist': still_exists,
                'name': track_name
            })

        # Sort removal details and warnings
        category_order = {'exact': 0, 'fuzzy': 1, 'internal': 2, 'unavailable': 3}
        removal_details_sorted = sorted(
            removal_details, 
            key=lambda x: (-x['score'], category_order.get(x['category'], 99))
        )
        cross_warnings_sorted = sorted(cross_warnings, key=lambda x: x[2], reverse=True)
        
        # Prepare warnings for template
        warnings_for_template = []
        for target_track, similar_track, score, reasons in cross_warnings_sorted:
            warnings_for_template.append({
                'name': target_track.get('name', 'Unknown'),
                'artists': ', '.join(a.get('name', '') for a in target_track.get('artists', [])),
                'similar_to': similar_track.get('name', 'Unknown'),
                'score': score,
                'reasons': ', '.join(reasons)
            })

        # Build results dict
        results = {
            'actual_removals': actual_removals,
            'unique_tracks': len(tracks_to_remove_ids),
            'unavailable_count': len(unavailable_tracks),
            'exact_count': len(exact_matches),
            'fuzzy_count': len(fuzzy_duplicates),
            'internal_count': len(internal_duplicates),
            'failed_count': len(failed_removals),
            'removal_details': removal_details_sorted,
            'warnings': warnings_for_template,
            'warnings_total': len(cross_warnings_sorted),
            'debug_log': removal_log,
            'verification': verification_results,
            'post_removal_count': len(post_removal_ids),
            'seven_rings_debug': seven_rings_debug
        }

        return render_template_string(HTML_RESULTS_PAGE,
            logo=LOGO_IMG,
            error=None,
            playlist_name=playlist_name,
            results=results
        )

    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()
        return render_template_string(HTML_RESULTS_PAGE,
            logo=LOGO_IMG,
            error=str(e),
            playlist_name="Unknown",
            results=None
        )


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


# --- DUPLICATE DETECTION HELPERS ---

def normalize_title(title):
    """Normalize a track title for comparison by removing version indicators."""
    if not title:
        return ""
    original = title
    title = title.lower()
    # Remove common suffixes that indicate versions - ORDER MATTERS (specific before generic)
    patterns = [
        # Specific remaster patterns first
        r'\s*\(remastered\s+album\s+version\)',
        r'\s*\(remastered\s+\d+\)',
        r'\s*\(remaster(ed)?\s*\d*\)',
        r'\s*[-–—]\s*remaster(ed)?(\s+\d+)?(\s+album\s+version)?',
        r'\s*[-–—]\s*\d+\s*remaster(ed)?',
        r'\s*\(remaster(ed)?(\s+\d+)?(\s+album\s+version)?\)',
        r'\s*\(\d+\s*remaster(ed)?\)',
        # Album/version indicators
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
        r'\s*\(edit\)',
        r'\s*\(re-?recorded.*?\)',
        r'\s*\(remix.*?\)',
        r'\s*[-–—]\s*remix.*$',
        r'\s*[-–—]\s*live.*$',
        r'\s*\(live.*?\)',
        r'\s*\(acoustic.*?\)',
        r'\s*[-–—]\s*acoustic.*$',
        r'\s*[-–—]\s*from\s+".*"',
        r'\s*\(from\s+".*"\)',
        r'\s*\(from\s+.*?\)',
        r'\s*[-–—]\s*mono.*$',
        r'\s*\(mono.*?\)',
        r'\s*[-–—]\s*stereo.*$',
        r'\s*\(stereo.*?\)',
        r'\s*\(super\s+deluxe.*?\)',
        r'\s*\(special\s+edition.*?\)',
        r'\s*\(version\)$',
        # Generic catch-alls LAST
        r'\s*\([^)]*remaster[^)]*\)',  # Anything with remaster in parens
        r'\s*\([^)]*version\)',  # Anything ending with version in parens
        r'\s*\([^)]*edition\)',  # Anything ending with edition in parens
        r'\s*\([^)]*mix\)',  # Anything ending with mix in parens
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
    Returns (score, reasons) where score >= 80 means duplicate, 40-79 means warning.
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
            if best_score >= 80:
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
                if score >= 80:
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
        .header .title { display: flex; align-items: center; gap: 1rem; }
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


    <form method="POST" action="{{ url_for('run_filter') }}" id="filter-form">
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
            <button type="submit" class="submit-btn" id="submit-btn">Start Filtering</button>
        </div>

        <div class="box filter-card">
            <h2>2. Filter Playlists</h2>
            <p>Select which songs to remove. Any song from these sources will be removed from your target playlist.</p>
            <div class="playlist-list">
                <label class="playlist-item">
                    <input type="checkbox" name="include_liked_songs" checked>
                    <div class="playlist-cover placeholder">
                        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="white"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>
                    </div>
                    <div class="playlist-info"><span class="playlist-name">Your Liked Songs</span></div>
                </label>
                {% for playlist in playlists %}
                <label class="playlist-item">
                    <input type="checkbox" name="filter_playlists" value="{{ playlist.id }}">
                    {% if playlist.images and playlist.images|length > 0 %}
                        <img src="{{ playlist.images[-1].url }}" alt="{{ playlist.name }} cover" class="playlist-cover">
                    {% else %}
                        <div class="playlist-cover placeholder"><span>🎵</span></div>
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
    </form>
    <script>
        document.getElementById('filter-form').addEventListener('submit', function() {
            var btn = document.getElementById('submit-btn');
            btn.disabled = true;
            btn.textContent = 'Filtering... (this may take a while)';
        });
    </script>
</body>
</html>
"""

HTML_RESULTS_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Filter Results - Spotify Filterer</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #121212; color: #fff; margin: 0; padding: 2rem;
            background: linear-gradient(-45deg, #121212, #191919, #0d2a14, #191919); background-size: 400% 400%; animation: gradientBG 25s ease infinite; }
        @keyframes gradientBG { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #282828; padding-bottom: 2rem; margin-bottom: 2rem; max-width: 1000px; margin-left: auto; margin-right: auto; }
        .header .title { display: flex; align-items: center; gap: 1rem; }
        .header h1 { margin: 0; }
        .back-btn { background: #333; color: white; text-decoration: none; padding: 0.75rem 1.5rem; border-radius: 500px; font-size: 1rem; font-weight: bold; }
        .back-btn:hover { background: #555; }
        .container { max-width: 1000px; margin: 0 auto; }
        .summary-box { background: #181818; padding: 2rem; border-radius: 1rem; margin-bottom: 2rem; }
        .summary-title { font-size: 1.5rem; color: #1DB954; margin-bottom: 1rem; }
        .summary-subtitle { color: #aaa; font-size: 0.95rem; margin-bottom: 1.5rem; }
        .stats { display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 1rem; }
        .stat { background: #282828; padding: 1rem 1.5rem; border-radius: 0.5rem; font-size: 1rem; }
        .results-box { background: #181818; padding: 2rem; border-radius: 1rem; margin-bottom: 2rem; }
        .results-box h3 { margin-top: 0; margin-bottom: 1rem; border-bottom: 1px solid #282828; padding-bottom: 0.5rem; }
        .song-list { max-height: 400px; overflow-y: auto; background: #121212; padding: 1rem; border-radius: 8px; list-style-type: none; margin: 0; }
        .song-list li { padding: 0.75rem 0; border-bottom: 1px solid #282828; }
        .song-list li:last-child { border-bottom: none; }
        .song-name { font-weight: bold; color: #fff; }
        .song-artists { color: #b3b3b3; }
        .song-reason { color: #888; font-size: 0.85rem; margin-top: 0.25rem; }
        .warning-box { border-left: 3px solid #FFA500; }
        .warning-box h3 { color: #FFA500; }
        .warning-box .song-list { max-height: none; }
        .error-box { background: #181818; padding: 2rem; border-radius: 1rem; border-left: 3px solid #FF4500; }
        .error-box h3 { color: #FF4500; margin-top: 0; }
    </style>
</head>
<body>
    <div class="header">
        <div class="title">{{ logo|safe }}<h1>Filter Results</h1></div>
        <a href="{{ url_for('index') }}" class="back-btn">← Back to Filter</a>
    </div>
    <div class="container">
        {% if error %}
        <div class="error-box"><h3>❌ An Error Occurred</h3><p>{{ error }}</p></div>
        {% elif results %}
        <div class="summary-box">
            <div class="summary-title">
                {% if results.actual_removals > 0 %}✅ Removed {{ results.actual_removals }} songs from "{{ playlist_name }}"
                {% else %}✅ No songs to remove from "{{ playlist_name }}"{% endif %}
            </div>
            {% if results.actual_removals != results.unique_tracks %}
            <div class="summary-subtitle">({{ results.unique_tracks }} unique tracks, {{ results.actual_removals - results.unique_tracks }} were duplicates in playlist)</div>
            {% endif %}
            <div class="stats">
                {% if results.unavailable_count > 0 %}<div class="stat">🚫 {{ results.unavailable_count }} unavailable</div>{% endif %}
                {% if results.exact_count > 0 %}<div class="stat">✓ {{ results.exact_count }} exact matches</div>{% endif %}
                {% if results.fuzzy_count > 0 %}<div class="stat">🔄 {{ results.fuzzy_count }} fuzzy duplicates</div>{% endif %}
                {% if results.internal_count > 0 %}<div class="stat">📋 {{ results.internal_count }} internal duplicates</div>{% endif %}
                {% if results.failed_count > 0 %}<div class="stat" style="color: #FF4500;">❌ {{ results.failed_count }} failed to remove</div>{% endif %}
            </div>
        </div>
        {% if results.removal_details %}
        <div class="results-box">
            <h3>Removed Songs ({{ results.removal_details|length }})</h3>
            <ul class="song-list">
                {% for song in results.removal_details %}
                <li><span class="song-name">{{ song.name }}</span><span class="song-artists"> - {{ song.artists }}</span><div class="song-reason">{{ song.reason }}</div></li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}
        {% if results.warnings %}
        <div class="results-box warning-box">
            <h3>⚠️ Potential Duplicates (not removed) - {{ results.warnings_total }} total</h3>
            <p style="color: #aaa; font-size: 0.9rem; margin-bottom: 1rem;">These songs are similar but didn't meet the threshold for automatic removal.</p>
            <ul class="song-list">
                {% for warning in results.warnings %}
                <li><span class="song-name">{{ warning.name }}</span><span class="song-artists"> - {{ warning.artists }}</span><div class="song-reason" style="color: #FFA500;">Similar to "{{ warning.similar_to }}" ({{ warning.score }}pts: {{ warning.reasons }})</div></li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}
        
        <div class="results-box" style="border-left: 3px solid #888;">
            <h3 style="color: #888;">🔍 Debug Info</h3>
            <p>Playlist after removal: {{ results.post_removal_count }} tracks</p>
            
            <h4>7 Rings Investigation:</h4>
            <ul class="song-list" style="font-family: monospace; font-size: 0.8rem; border-left: 3px solid #FF4500;">
                {% for log in results.seven_rings_debug %}
                <li>{{ log }}</li>
                {% endfor %}
                {% if not results.seven_rings_debug %}
                <li>No 7 rings debug info captured</li>
                {% endif %}
            </ul>
            
            <h4>Verification (sample of 5 tracks):</h4>
            <ul class="song-list">
                {% for v in results.verification %}
                <li style="color: {% if v.still_in_playlist %}#FF4500{% else %}#1DB954{% endif %};">
                    ID: {{ v.id }}... - {% if v.still_in_playlist %}❌ STILL IN PLAYLIST{% else %}✅ Removed{% endif %}
                </li>
                {% endfor %}
            </ul>
            <h4>API Log:</h4>
            <ul class="song-list" style="font-family: monospace; font-size: 0.8rem;">
                {% for log in results.debug_log %}
                <li>{{ log }}</li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""

# This makes the app runnable locally for testing (python app.py)
# Vercel will use a different method to run the 'app' object
if __name__ == "__main__":
    app.run(debug=True, port=8080)