(function () {
  'use strict';

  var config = JSON.parse(document.getElementById('page-config').textContent);
  var LOCATIONS_URL = config.locationsUrl;

  // ── Autocomplete ──────────────────────────────────────────────────────────

  function setupAutocomplete(inputId, dropdownId) {
    var input    = document.getElementById(inputId);
    var dropdown = document.getElementById(dropdownId);
    var debounce, query = '', offset = 0, total = 0, loading = false;

    function close() {
      dropdown.classList.add('hidden');
      dropdown.innerHTML = '';
      offset = 0;
      total  = 0;
    }

    function fetchPage(resetList) {
      if (loading) return;
      if (!resetList && offset >= total && total > 0) return;
      loading = true;

      var url = LOCATIONS_URL + '?q=' + encodeURIComponent(query) +
                '&offset=' + (resetList ? 0 : offset) + '&limit=20';

      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          loading = false;
          if (resetList) {
            dropdown.innerHTML = '';
            offset = 0;
          }
          total = data.total;

          if (data.locations.length === 0 && offset === 0) {
            dropdown.innerHTML = '<div class="ac-empty">No cities found</div>';
            dropdown.classList.remove('hidden');
            return;
          }

          data.locations.forEach(function (loc) {
            var item = document.createElement('div');
            item.className = 'ac-item';
            item.textContent = loc.label;
            item.addEventListener('mousedown', function (e) {
              e.preventDefault();
              input.value = loc.value;
              close();
            });
            dropdown.appendChild(item);
          });

          offset += data.locations.length;
          dropdown.classList.remove('hidden');
        })
        .catch(function () { loading = false; });
    }

    input.addEventListener('input', function () {
      clearTimeout(debounce);
      var val = input.value.trim();
      if (val.length < 2) { close(); return; }
      debounce = setTimeout(function () {
        query = val;
        fetchPage(true);
      }, 250);
    });

    input.addEventListener('focus', function () {
      if (input.value.trim().length >= 2 && dropdown.innerHTML === '') {
        query = input.value.trim();
        fetchPage(true);
      }
    });

    dropdown.addEventListener('scroll', function () {
      if (dropdown.scrollHeight - dropdown.scrollTop <= dropdown.clientHeight + 60) {
        fetchPage(false);
      }
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { close(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        var items = dropdown.querySelectorAll('.ac-item');
        if (!items.length) return;
        var active = dropdown.querySelector('.ac-item.active');
        var idx = -1;
        items.forEach(function (el, i) { if (el === active) idx = i; });
        if (active) active.classList.remove('active');
        if (e.key === 'ArrowDown') idx = Math.min(idx + 1, items.length - 1);
        else idx = Math.max(idx - 1, 0);
        items[idx].classList.add('active');
        items[idx].scrollIntoView({ block: 'nearest' });
      }
      if (e.key === 'Enter') {
        var active = dropdown.querySelector('.ac-item.active');
        if (active) {
          e.preventDefault();
          input.value = active.textContent;
          close();
        }
      }
    });

    document.addEventListener('click', function (e) {
      if (!input.contains(e.target) && !dropdown.contains(e.target)) {
        close();
      }
    });
  }

  setupAutocomplete('start',  'start-dropdown');
  setupAutocomplete('finish', 'finish-dropdown');

  // ── Helpers ───────────────────────────────────────────────────────────────

  function getCsrfToken() {
    var cookies = document.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
      var c = cookies[i].trim();
      if (c.startsWith('csrftoken=')) {
        return decodeURIComponent(c.slice('csrftoken='.length));
      }
    }
    return '';
  }

  function fmt(n, decimals) { return Number(n).toFixed(decimals); }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function decodePolyline(encoded) {
    var points = [], index = 0, lat = 0, lng = 0;
    while (index < encoded.length) {
      var shift = 0, result = 0, b;
      do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
      lat += (result & 1) ? ~(result >> 1) : (result >> 1);
      shift = 0; result = 0;
      do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
      lng += (result & 1) ? ~(result >> 1) : (result >> 1);
      points.push([lat / 1e5, lng / 1e5]);
    }
    return points;
  }

  // ── Form & results ────────────────────────────────────────────────────────

  var form        = document.getElementById('route-form');
  var btn         = document.getElementById('submit-btn');
  var errorBanner = document.getElementById('error-banner');
  var resultsDiv  = document.getElementById('results');
  var summaryStrip = document.getElementById('summary-strip');
  var stopsTbody  = document.getElementById('stops-tbody');
  var mapPageLink = document.getElementById('map-page-link');
  var leafletMap  = null;

  function showError(msg) {
    errorBanner.textContent = msg;
    errorBanner.classList.remove('hidden');
    resultsDiv.classList.add('hidden');
  }

  function hideError() { errorBanner.classList.add('hidden'); }

  function renderResults(data) {
    hideError();

    summaryStrip.innerHTML =
      '<div class="summary-item"><span class="summary-label">Distance</span>' +
      '<span class="summary-value">' + fmt(data.total_distance_miles, 1) + ' mi</span></div>' +
      '<div class="summary-item"><span class="summary-label">Total gallons</span>' +
      '<span class="summary-value">' + fmt(data.fuel.total_gallons, 1) + '</span></div>' +
      '<div class="summary-item"><span class="summary-label">Fuel cost</span>' +
      '<span class="summary-value">$' + fmt(data.fuel.total_fuel_cost_usd, 2) + '</span></div>' +
      '<div class="summary-item"><span class="summary-label">Stops</span>' +
      '<span class="summary-value">' + data.fuel.stops.length + '</span></div>';

    stopsTbody.innerHTML = '';
    data.fuel.stops.forEach(function (s, i) {
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + (i + 1) + '</td>' +
        '<td>' + escapeHtml(s.name) + '</td>' +
        '<td>' + escapeHtml(s.city) + ', ' + escapeHtml(s.state) + '</td>' +
        '<td>' + fmt(s.route_position_miles, 1) + '</td>' +
        '<td>$' + fmt(s.price_per_gallon, 3) + '</td>' +
        '<td>' + fmt(s.gallons_purchased, 1) + '</td>' +
        '<td>$' + fmt(s.cost_usd, 2) + '</td>';
      stopsTbody.appendChild(tr);
    });

    if (data.map_url) mapPageLink.href = data.map_url;

    resultsDiv.classList.remove('hidden');

    if (leafletMap) { leafletMap.remove(); leafletMap = null; }
    leafletMap = L.map('map');
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18
    }).addTo(leafletMap);

    var coords = decodePolyline(data.route_geometry);
    var poly = L.polyline(coords, { color: '#1a6fc4', weight: 4 }).addTo(leafletMap);

    function stopIcon(n) {
      return L.divIcon({ className: 'stop-icon', html: '<span>' + n + '</span>', iconSize: [24, 24], iconAnchor: [12, 12] });
    }

    data.fuel.stops.forEach(function (s, i) {
      var popup = '<b>' + escapeHtml(s.name) + '</b><br>' +
        escapeHtml(s.city) + ', ' + escapeHtml(s.state) + '<br>' +
        '$' + fmt(s.price_per_gallon, 3) + '/gal &nbsp;' +
        fmt(s.gallons_purchased, 1) + ' gal &nbsp;$' + fmt(s.cost_usd, 2);
      L.marker([s.lat, s.lng], { icon: stopIcon(i + 1) }).addTo(leafletMap).bindPopup(popup);
    });

    L.marker([data.start.lat, data.start.lng], {
      icon: L.divIcon({ className: 'stop-icon start-icon', html: 'S', iconSize: [24, 24], iconAnchor: [12, 12] })
    }).addTo(leafletMap).bindPopup('<b>Start</b><br>' + escapeHtml(data.start.resolved_city));

    L.marker([data.finish.lat, data.finish.lng], {
      icon: L.divIcon({ className: 'stop-icon end-icon', html: 'F', iconSize: [24, 24], iconAnchor: [12, 12] })
    }).addTo(leafletMap).bindPopup('<b>Finish</b><br>' + escapeHtml(data.finish.resolved_city));

    leafletMap.fitBounds(poly.getBounds(), { padding: [30, 30] });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var start  = document.getElementById('start').value.trim();
    var finish = document.getElementById('finish').value.trim();

    if (!start || !finish) {
      showError('Please enter both a start and a finish location.');
      return;
    }

    btn.disabled = true;
    btn.textContent = 'Calculating...';
    hideError();

    fetch(config.apiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
      body: JSON.stringify({ start: start, finish: finish })
    })
    .then(function (resp) {
      return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
    })
    .then(function (result) {
      if (result.ok) {
        renderResults(result.data);
      } else {
        var err    = result.data.error || {};
        var msg    = err.message || 'Something went wrong. Please try again.';
        var detail = err.detail || {};
        if (detail.suggestions && detail.suggestions.length) {
          msg += ' Did you mean: ' + detail.suggestions.join(', ') + '?';
        }
        if (detail.hint) msg += ' Hint: ' + detail.hint;
        showError(msg);
      }
    })
    .catch(function () {
      showError('Could not reach the server. Please check your connection and try again.');
    })
    .finally(function () {
      btn.disabled = false;
      btn.textContent = 'Calculate route';
    });
  });
}());
