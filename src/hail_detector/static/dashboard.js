const RADAR_BOUNDS = [[40.674, 20.678], [44.455, 25.822]];
const DEFAULT_TARGET = { latitude: 42.6977, longitude: 23.3219, warning_radius_km: 10 };

const map = L.map("map", { zoomControl: true }).setView([42.6977, 23.3219], 8);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 14,
  attribution: "© OpenStreetMap contributors",
}).addTo(map);
const layers = L.layerGroup().addTo(map);
const mapTrackLocations = new Map();
let focusedTrackId = null;
const cellsByFrame = new Map();
let dashboardData = null;
let historyFrames = [];
let isLiveTimeline = true;
let selectedHistoryFilename = null;
let timelineRenderVersion = 0;

function configureMapGestures() {
  const container = map.getContainer();
  // Leaflet listens in the bubbling phase. Stopping ordinary wheel events in
  // capture lets the page scroll normally, while Ctrl/Command-wheel reaches
  // Leaflet and retains its familiar map-zoom behaviour.
  container.addEventListener("wheel", (event) => {
    if (!event.ctrlKey && !event.metaKey) event.stopImmediatePropagation();
  }, { capture: true, passive: true });

  const isTouchDevice = window.matchMedia("(hover: none) and (pointer: coarse)").matches;
  if (!isTouchDevice) return;

  map.dragging.disable();
  const updateTouchDragging = (event) => {
    if (event.touches.length >= 2) {
      map.dragging.enable();
      event.preventDefault();
    } else {
      map.dragging.disable();
    }
  };
  container.addEventListener("touchstart", updateTouchDragging, { capture: true, passive: false });
  container.addEventListener("touchmove", updateTouchDragging, { capture: true, passive: false });
  container.addEventListener("touchend", updateTouchDragging, { capture: true, passive: false });
  container.addEventListener("touchcancel", () => map.dragging.disable(), { capture: true, passive: true });
}

configureMapGestures();

function focusMapOnTrack(trackId) {
  const locations = mapTrackLocations.get(trackId);
  if (!locations?.length) return;
  focusedTrackId = trackId;
  document.querySelectorAll(".track-card[data-track-id]").forEach((card) => {
    card.classList.toggle("is-focused", card.dataset.trackId === trackId);
  });
  map.invalidateSize();
  if (locations.length === 1) {
    map.setView(locations[0], Math.max(map.getZoom(), 10), { animate: true });
    return;
  }
  map.fitBounds(L.latLngBounds(locations).pad(0.16), { animate: true, maxZoom: 10 });
}

function escapeHtml(value) {
  return String(value ?? "—").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[character]));
}

function ageLabel(seconds) {
  if (seconds == null) return "Unknown";
  const rounded = Math.max(0, Math.round(seconds));
  if (rounded < 60) return `${rounded}s`;
  if (rounded < 3600) return `${Math.floor(rounded / 60)}m`;
  return `${(rounded / 3600).toFixed(1)}h`;
}

function liveAgeLabel(observedAt, fallbackSeconds) {
  const observed = new Date(observedAt || "");
  if (!Number.isNaN(observed.valueOf())) return ageLabel((Date.now() - observed.valueOf()) / 1000);
  return ageLabel(fallbackSeconds);
}

function dateLabel(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function predictionLabel(minutes) {
  return minutes % 60 === 0 ? `+${minutes / 60}h` : `+${minutes}m`;
}

function dbzLabel(lower, upper) {
  return Number.isFinite(lower) && Number.isFinite(upper) ? `${lower}-${upper} dBZ` : "dBZ unavailable";
}

function maximumDbzLabel(upper) {
  return Number.isFinite(upper) ? `${upper} dBZ` : "dBZ unavailable";
}

function currentDbzLabel(track) {
  const [lower, upper] = track.latest_max_dbz_band || [];
  return dbzLabel(lower, upper);
}

const INTENSITY_CHART_MIN_DBZ = 25;
const INTENSITY_CHART_MAX_DBZ = 70;

function timeOnlyLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? "Unknown"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderIntensityHistory(track) {
  const samples = (track.points || []).map((point) => ({
    observedAt: point.observed_at,
    lower: Number(point.max_dbz_lower),
    upper: Number(point.max_dbz_upper),
  })).filter((point) => Number.isFinite(point.lower) && Number.isFinite(point.upper));
  if (!samples.length) return '<p class="muted">No reflectivity history is available yet.</p>';

  const width = 320;
  const height = 146;
  const plot = { left: 34, right: 10, top: 12, bottom: 25 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const timestamps = samples.map((sample) => new Date(sample.observedAt).valueOf());
  const firstTimestamp = timestamps[0];
  const lastTimestamp = timestamps.at(-1);
  const xFor = (timestamp, index) => {
    if (!Number.isFinite(timestamp) || !Number.isFinite(firstTimestamp) || firstTimestamp === lastTimestamp) {
      return plot.left + (samples.length === 1 ? plotWidth / 2 : index / (samples.length - 1) * plotWidth);
    }
    return plot.left + (timestamp - firstTimestamp) / (lastTimestamp - firstTimestamp) * plotWidth;
  };
  const yFor = (value) => plot.top + (INTENSITY_CHART_MAX_DBZ - value)
    / (INTENSITY_CHART_MAX_DBZ - INTENSITY_CHART_MIN_DBZ) * plotHeight;
  const chartSamples = samples.map((sample, index) => ({
    ...sample,
    x: xFor(timestamps[index], index),
    // Stored values are bands, not an exact maximum. Plotting the lower edge
    // is conservative and matches the dBZ labels on the map.
    y: yFor(Math.max(INTENSITY_CHART_MIN_DBZ, Math.min(INTENSITY_CHART_MAX_DBZ, sample.lower))),
  }));
  const grid = [];
  for (let dbz = INTENSITY_CHART_MIN_DBZ; dbz <= INTENSITY_CHART_MAX_DBZ; dbz += 5) {
    const y = yFor(dbz);
    grid.push(`<line x1="${plot.left}" y1="${y}" x2="${width - plot.right}" y2="${y}" stroke="#d6d4ce" stroke-width="0.8" stroke-dasharray="2 3" /><text x="${plot.left - 5}" y="${y + 3}" text-anchor="end" fill="#696762" font-size="9">${dbz}</text>`);
  }
  const path = chartSamples.map((sample, index) => `${index ? "L" : "M"}${sample.x.toFixed(2)},${sample.y.toFixed(2)}`).join(" ");
  const markers = chartSamples.map((sample, index) => {
    const isCurrent = index === chartSamples.length - 1;
    const fill = isCurrent ? "#d9682f" : "#97948d";
    const stroke = isCurrent ? "#101010" : "#ffffff";
    return `<circle cx="${sample.x}" cy="${sample.y}" r="${isCurrent ? 4 : 3}" fill="${fill}" stroke="${stroke}" stroke-width="${isCurrent ? 1.5 : 1}"><title>${escapeHtml(`${timeOnlyLabel(sample.observedAt)} - ${dbzLabel(sample.lower, sample.upper)}`)}</title></circle>`;
  }).join("");
  return `<figure class="intensity-history"><figcaption>Intensity history (dBZ)</figcaption><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Maximum reflectivity history from ${escapeHtml(timeOnlyLabel(samples[0].observedAt))} to ${escapeHtml(timeOnlyLabel(samples.at(-1).observedAt))}"><g>${grid.join("")}</g><path d="${path}" fill="none" stroke="#101010" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" />${markers}<text x="${plot.left}" y="${height - 6}" fill="#696762" font-size="9">${escapeHtml(timeOnlyLabel(samples[0].observedAt))}</text><text x="${width - plot.right}" y="${height - 6}" text-anchor="end" fill="#696762" font-size="9">${escapeHtml(timeOnlyLabel(samples.at(-1).observedAt))}</text></svg></figure>`;
}

function rollingThreePointAverage(points) {
  if (points.length < 3) return points;
  return points.map((point, index) => {
    // Preserve both endpoints so the path begins and ends at the observed
    // positions; average only the interior position with its neighbours.
    if (index === 0 || index === points.length - 1) return point;
    const before = points[index - 1];
    const after = points[index + 1];
    return [
      (before[0] + point[0] + after[0]) / 3,
      (before[1] + point[1] + after[1]) / 3,
    ];
  });
}

function catmullRomValue(previous, start, end, next, amount) {
  const amountSquared = amount * amount;
  const amountCubed = amountSquared * amount;
  return 0.5 * (
    (2 * start)
    + (-previous + end) * amount
    + (2 * previous - 5 * start + 4 * end - next) * amountSquared
    + (-previous + 3 * start - 3 * end + next) * amountCubed
  );
}

function splineThrough(points, samplesPerSegment = 8) {
  if (points.length < 3) return points;
  const curve = [];
  for (let index = 0; index < points.length - 1; index += 1) {
    const previous = points[Math.max(0, index - 1)];
    const start = points[index];
    const end = points[index + 1];
    const next = points[Math.min(points.length - 1, index + 2)];
    for (let sample = 0; sample < samplesPerSegment; sample += 1) {
      const amount = sample / samplesPerSegment;
      curve.push([
        catmullRomValue(previous[0], start[0], end[0], next[0], amount),
        catmullRomValue(previous[1], start[1], end[1], next[1], amount),
      ]);
    }
  }
  curve.push(points.at(-1));
  return curve;
}

async function getJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}

function historyFrameForFilename(filename) {
  return historyFrames.find((frame) => frame.filename === filename) || null;
}

function selectedMapFrame() {
  if (!dashboardData) return null;
  if (isLiveTimeline) return dashboardData.status.latest_dbzh_composite || null;
  return historyFrameForFilename(selectedHistoryFilename);
}

function updateTimelineControls() {
  const slider = document.getElementById("timelineSlider");
  const liveButton = document.getElementById("liveButton");
  const label = document.getElementById("timelineLabel");
  const selected = selectedMapFrame();
  const selectedIndex = historyFrames.findIndex((frame) => frame.filename === selected?.filename);

  slider.disabled = historyFrames.length === 0;
  slider.min = "0";
  slider.max = String(Math.max(0, historyFrames.length - 1));
  if (selectedIndex >= 0) slider.value = String(selectedIndex);
  slider.setAttribute("aria-valuetext", selected ? dateLabel(selected.observed_at) : "No archived OPERA frames");
  liveButton.classList.toggle("is-live", isLiveTimeline);
  label.textContent = selected
    ? `${isLiveTimeline ? "LIVE" : "Frame"} — ${dateLabel(selected.observed_at)}`
    : "No archived OPERA frames";
}

async function cellsForFrame(frame) {
  if (!frame?.filename) return null;
  if (!cellsByFrame.has(frame.filename)) {
    cellsByFrame.set(
      frame.filename,
      getJson(`/opera/cells/${encodeURIComponent(frame.filename)}`).catch(() => null),
    );
  }
  return cellsByFrame.get(frame.filename);
}

async function renderTimelineMap() {
  const renderVersion = ++timelineRenderVersion;
  const frame = selectedMapFrame();
  if (!dashboardData || !frame) {
    drawMap(dashboardData?.status || {}, dashboardData?.tracker || {}, null, {
      displayFrame: null,
      historical: !isLiveTimeline,
    });
    return;
  }
  const cells = isLiveTimeline ? dashboardData.liveCells : await cellsForFrame(frame);
  if (renderVersion !== timelineRenderVersion) return;
  drawMap(dashboardData.status, dashboardData.tracker, cells, {
    displayFrame: frame,
    historical: !isLiveTimeline,
  });
}

function selectHistoryFrame(index) {
  const frame = historyFrames[index];
  if (!frame) return;
  isLiveTimeline = false;
  selectedHistoryFilename = frame.filename;
  updateTimelineControls();
  void renderTimelineMap();
}

function selectLiveTimeline() {
  isLiveTimeline = true;
  selectedHistoryFilename = null;
  updateTimelineControls();
  void renderTimelineMap();
}

function setText(id, value) { document.getElementById(id).textContent = value; }

function renderStatus(status) {
  const state = status.state || "starting";
  const badge = document.getElementById("stateBadge");
  badge.className = `badge ${state}`;
  badge.textContent = state.toUpperCase();
  const operaFrame = status.latest_dbzh_composite || {};
  setText("radarAge", liveAgeLabel(operaFrame.observed_at, status.latest_age_seconds));
  const volume = status.volume || {};
  const volumeFrame = volume.latest_dbzh_volume || {};
  setText(
    "volumeAge",
    volumeFrame.observed_at ? liveAgeLabel(volumeFrame.observed_at, volume.latest_age_seconds) : volume.state || "Waiting",
  );
  setText("trackCount", status.active_tracks ?? 0);
  setText("intersectionCount", status.intersecting_target_tracks ?? 0);
  setText("alertCount", status.active_alerts ?? 0);
  const notice = document.getElementById("staleNotice");
  if (state !== "ready") {
    notice.classList.remove("hidden");
    notice.textContent = `OPERA primary observations are unavailable: ${status.error || "the pipeline is not ready"}. Tracking and automatic hail alerts remain paused.`;
  } else {
    notice.classList.add("hidden");
  }
  setText(
    "sourceSummary",
    operaFrame.observed_at ? `OPERA observed ${dateLabel(operaFrame.observed_at)}` : "Waiting for OPERA data",
  );
}

function assessmentIndex(assessments) {
  return new Map((assessments.assessments || []).map((item) => [item.track_id, item]));
}

function distanceFromTargetKm(location, target) {
  const latitude = Number(location?.latitude);
  const longitude = Number(location?.longitude);
  const targetLatitude = Number(target?.latitude);
  const targetLongitude = Number(target?.longitude);
  if (![latitude, longitude, targetLatitude, targetLongitude].every(Number.isFinite)) {
    return Number.POSITIVE_INFINITY;
  }

  const toRadians = (degrees) => degrees * Math.PI / 180;
  const latitudeDelta = toRadians(targetLatitude - latitude);
  const longitudeDelta = toRadians(targetLongitude - longitude);
  const haversine = Math.sin(latitudeDelta / 2) ** 2
    + Math.cos(toRadians(latitude)) * Math.cos(toRadians(targetLatitude))
    * Math.sin(longitudeDelta / 2) ** 2;
  return 6_371 * 2 * Math.atan2(Math.sqrt(haversine), Math.sqrt(1 - haversine));
}

function latestTrackLocation(track) {
  return track.latest_centroid || track.points?.at(-1)?.centroid_location;
}

function renderVolumeProfile(volume, trackId) {
  const profile = (volume?.track_profiles || []).find((item) => item.track_id === trackId);
  const source = volume?.radar?.name || "OPERA volume radar";
  if (!profile) {
    return `<p class="volume-profile muted">Experimental volume: ${escapeHtml(source)} has no profile for this track yet.</p>`;
  }
  const samples = (profile.profile || []).filter((sample) => Number.isFinite(sample.max_dbzh));
  if (!samples.length) {
    return `<p class="volume-profile muted">Experimental volume: no sampled reflectivity below the configured beam-height limit.</p>`;
  }
  const values = samples.map((sample) => {
    const height = Number.isFinite(sample.beam_height_metres_msl)
      ? (sample.beam_height_metres_msl / 1000).toFixed(1)
      : "?";
    return `${height} km: ${sample.max_dbzh.toFixed(0)} dBZ`;
  }).join(" · ");
  const distance = Number.isFinite(profile.distance_km) ? profile.distance_km.toFixed(0) : "unknown";
  return `<div class="volume-profile"><p><strong>Experimental vertical profile</strong><span>${escapeHtml(source)} · ${escapeHtml(distance)} km away</span></p><p>${escapeHtml(values)}</p><small>Local 3 × 3 radar-gate maximum; not used in tracking or forecasts.</small></div>`;
}

const VOLUME_PROFILE_MAXIMUM_HEIGHT_KM = 20;

function volumeColor(maxDbzh) {
  if (maxDbzh >= 60) return "#c43e45";
  if (maxDbzh >= 55) return "#d9682f";
  if (maxDbzh >= 50) return "#e58e2d";
  if (maxDbzh >= 45) return "#e8ad2d";
  if (maxDbzh >= 40) return "#75ae63";
  if (maxDbzh >= 35) return "#4c9c83";
  return "#79aabf";
}

function renderVerticalProfile(volume, trackId) {
  const profile = (volume?.track_profiles || []).find((item) => item.track_id === trackId);
  const source = volume?.radar?.name || "OPERA volume radar";
  const distance = Number.isFinite(profile?.distance_km) ? `${profile.distance_km.toFixed(0)} km away` : "distance unavailable";
  const samples = (profile?.profile || []).map((sample) => ({
    heightKm: Number(sample.beam_height_metres_msl) / 1000,
    maxDbzh: Number(sample.max_dbzh),
  })).filter((sample) => Number.isFinite(sample.heightKm) && Number.isFinite(sample.maxDbzh)
    && sample.heightKm >= 0 && sample.heightKm <= VOLUME_PROFILE_MAXIMUM_HEIGHT_KM);
  const caption = `<figcaption><span>Vertical radar sample</span><small>${escapeHtml(source)} · ${escapeHtml(distance)}</small></figcaption>`;
  if (!samples.length) {
    return `<figure class="vertical-profile vertical-profile--empty">${caption}<div class="vertical-profile__empty">No sampled volume data</div><small class="vertical-profile__note">Experimental remote-radar observation</small></figure>`;
  }

  const width = 104;
  const height = 146;
  const plot = { left: 25, right: 9, top: 12, bottom: 25 };
  const plotHeight = height - plot.top - plot.bottom;
  const yFor = (heightKm) => plot.top + (VOLUME_PROFILE_MAXIMUM_HEIGHT_KM - heightKm)
    / VOLUME_PROFILE_MAXIMUM_HEIGHT_KM * plotHeight;
  const grid = [];
  for (let heightKm = 0; heightKm <= VOLUME_PROFILE_MAXIMUM_HEIGHT_KM; heightKm += 5) {
    const y = yFor(heightKm);
    grid.push(`<line x1="${plot.left}" y1="${y}" x2="${width - plot.right}" y2="${y}" stroke="#d6d4ce" stroke-width="0.8" stroke-dasharray="2 3" /><text x="${plot.left - 4}" y="${y + 3}" text-anchor="end" fill="#696762" font-size="8">${heightKm}</text>`);
  }
  const coreLeft = 53;
  const bands = samples.map((sample) => {
    const widthForValue = 13 + Math.min(1, Math.max(0, sample.maxDbzh / 70)) * 20;
    const x = coreLeft - widthForValue / 2;
    const y = yFor(sample.heightKm) - 3.5;
    const label = `${sample.heightKm.toFixed(1)} km MSL: ${sample.maxDbzh.toFixed(0)} dBZ`;
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${widthForValue.toFixed(1)}" height="7" rx="2" fill="${volumeColor(sample.maxDbzh)}" stroke="#ffffff" stroke-width="0.9"><title>${escapeHtml(label)}</title></rect>`;
  }).join("");
  const label = `Experimental vertical radar sample from 0 to ${VOLUME_PROFILE_MAXIMUM_HEIGHT_KM} kilometres MSL`;
  return `<figure class="vertical-profile">${caption}<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}"><g>${grid.join("")}</g><line x1="${coreLeft}" y1="${plot.top}" x2="${coreLeft}" y2="${height - plot.bottom}" stroke="#bbb8b0" stroke-width="0.8" />${bands}<text x="${width - plot.right}" y="${height - 6}" text-anchor="end" fill="#696762" font-size="8">km MSL</text></svg><small class="vertical-profile__note">Local 3 by 3 gate maximum · experimental only</small></figure>`;
}

function volumeEchoTop(profile) {
  const echoes = (profile?.profile || []).filter((sample) =>
    Number.isFinite(sample.max_dbzh)
    && sample.max_dbzh >= 35
    && Number.isFinite(sample.beam_height_metres_msl));
  return echoes.reduce(
    (highest, sample) => (!highest || sample.beam_height_metres_msl > highest.beam_height_metres_msl ? sample : highest),
    null,
  );
}

function volumeEchoTopLabel(sample) {
  if (!sample) return null;
  return `Volume >=35 dBZ: ${(sample.beam_height_metres_msl / 1000).toFixed(1)} km MSL`;
}

function renderTracks(tracker, assessments, volume) {
  const index = assessmentIndex(assessments);
  const minimumConfirmations = tracker.minimum_confirmed_observations || 1;
  const target = tracker.target || DEFAULT_TARGET;
  const tracks = (tracker.tracks || []).filter((track) =>
    (track.point_count ?? track.points?.length ?? 0) >= minimumConfirmations)
    .sort((first, second) => {
      const firstDistance = distanceFromTargetKm(latestTrackLocation(first), target);
      const secondDistance = distanceFromTargetKm(latestTrackLocation(second), target);
      if (firstDistance !== secondDistance) return firstDistance - secondDistance;
      return String(first.track_id).localeCompare(String(second.track_id));
    });
  const list = document.getElementById("trackList");
  if (!tracks.length) { list.innerHTML = '<p class="muted">No segmented cells have been tracked yet.</p>'; return; }
  list.innerHTML = tracks.map((track) => {
    const assessment = index.get(track.track_id) || {};
    const level = assessment.candidate_level || assessment.radar_signature || "OPERA observation";
    const style = assessment.candidate_level === "Severe Reflectivity Alert" ? "severe" : "";
    const forecast = (tracker.forecasts || []).find((item) => item.track_id === track.track_id);
    const hasMotionVector = Number.isFinite(track.speed_kmh) && Number.isFinite(track.bearing_degrees);
    const motion = hasMotionVector
      ? `${track.speed_kmh.toFixed(0)} km/h, ${track.bearing_degrees.toFixed(0)}°`
      : track.speed_kmh === 0
        ? "No resolved movement"
        : "Motion needs another observation";
    const arrival = forecast?.arrival_window_start ? `Target entry: ${dateLabel(forecast.arrival_window_start)}` : "No target intersection forecast";
    const projection = (forecast?.predicted_positions || []).map((position) => predictionLabel(position.lead_minutes)).join(", ") || "No motion projection";
    const reason = (assessment.reasons || []).slice(-1)[0] || "OPERA maximum-reflectivity observation; no severe alert candidate.";
    const focused = focusedTrackId === track.track_id ? "is-focused" : "";
    return `<article class="track-card ${style} ${focused}" data-track-id="${escapeHtml(track.track_id)}" tabindex="0" role="button" aria-label="Focus the map on this storm track"><h3>${escapeHtml(level)}</h3><p>Current reflectivity: ${escapeHtml(currentDbzLabel(track))}</p><div class="observation-charts">${renderIntensityHistory(track)}${renderVerticalProfile(volume, track.track_id)}</div><p>${escapeHtml(motion)}</p><p>Projection: ${escapeHtml(projection)}</p><p>${escapeHtml(arrival)}</p><p class="reason">${escapeHtml(reason)}</p></article>`;
  }).join("");
}

function drawMap(status, tracker, cells, { displayFrame = null, historical = false } = {}) {
  layers.clearLayers();
  mapTrackLocations.clear();
  const target = tracker.target || DEFAULT_TARGET;
  const operaFrame = displayFrame || status.latest_dbzh_composite;
  if (operaFrame?.filename) {
    L.imageOverlay(`/opera/overlay/${encodeURIComponent(operaFrame.filename)}`, RADAR_BOUNDS, { opacity: 0.72, interactive: false }).addTo(layers);
  }
  L.circle([target.latitude, target.longitude], {
    radius: target.warning_radius_km * 1000, color: "#16733a", weight: 2, fillOpacity: 0.08,
  }).bindPopup(`Configured target radius: ${target.warning_radius_km} km`).addTo(layers);
  for (const cell of cells?.cells || []) {
    const location = cell.centroid_location;
    const radius = Math.max(4, Math.min(15, Math.sqrt(cell.area_square_km || 0) * 5));
    const area = Number.isFinite(cell.area_square_km) ? `${cell.area_square_km.toFixed(1)} km²` : "area unavailable";
    L.circleMarker([location.latitude, location.longitude], { radius, color: "#a66e00", fillColor: "#e8ad2d", fillOpacity: 0.8, weight: 1 })
      .bindPopup(`${cell.max_band?.label || "Unknown"} dBZ · ${area}`).addTo(layers);
  }
  const volumeProfiles = new Map(
    !historical
      ? (status.volume?.track_profiles || []).map((profile) => [profile.track_id, profile])
      : [],
  );
  const volumeSource = !historical ? [...volumeProfiles.values()][0] : null;
  const volumeRadarLocation = volumeSource?.radar_location;
  if (Number.isFinite(volumeRadarLocation?.latitude) && Number.isFinite(volumeRadarLocation?.longitude)) {
    const sourceName = escapeHtml(status.volume?.radar?.name || "OPERA volume radar");
    L.circleMarker([volumeRadarLocation.latitude, volumeRadarLocation.longitude], {
      radius: 5,
      color: "#ffffff",
      fillColor: "#245f8f",
      fillOpacity: 1,
      weight: 2,
    })
      .bindTooltip(`Experimental volume radar: ${sourceName}`, { direction: "top" })
      .addTo(layers);
  }

  const minimumConfirmations = tracker.minimum_confirmed_observations || 1;
  const cutoff = historical ? new Date(operaFrame?.observed_at || "").valueOf() : null;
  for (const track of tracker.tracks || []) {
    if ((track.point_count ?? track.points?.length ?? 0) < minimumConfirmations) continue;
    const visiblePoints = (track.points || []).filter((point) => {
      if (!historical || !Number.isFinite(cutoff)) return true;
      return new Date(point.observed_at || "").valueOf() <= cutoff;
    });
    const rawPoints = visiblePoints.map((point) => [point.centroid_location.latitude, point.centroid_location.longitude]);
    if (!rawPoints.length) continue;
    mapTrackLocations.set(track.track_id, rawPoints);
    const path = splineThrough(rollingThreePointAverage(rawPoints));
    if (path.length > 1) L.polyline(path, { color: "#d9682f", weight: 3 }).addTo(layers);
    for (const point of visiblePoints.slice(0, -1)) {
      const location = [point.centroid_location.latitude, point.centroid_location.longitude];
      const label = maximumDbzLabel(Number(point.max_dbz_upper));
      L.circleMarker(location, { radius: 3.5, color: "#ffffff", fillColor: "#7d7a73", fillOpacity: 0.92, opacity: 0.95, weight: 1 })
        .bindTooltip(label, { permanent: true, direction: "top", className: "intensity-breadcrumb", offset: [0, -2] })
        .addTo(layers);
    }
    const latest = rawPoints.at(-1);
    const latestPoint = visiblePoints.at(-1);
    const volumeTop = volumeEchoTop(volumeProfiles.get(track.track_id));
    const volumeLabel = volumeEchoTopLabel(volumeTop);
    if (volumeLabel) {
      L.circleMarker(latest, {
        radius: 10, color: "#245f8f", fillOpacity: 0, weight: 2, opacity: 0.9, interactive: false,
      }).addTo(layers);
    }
    const currentLabel = `${dbzLabel(latestPoint.max_dbz_lower, latestPoint.max_dbz_upper)}${
      volumeLabel ? `<span class="volume-detail">${volumeLabel}</span>` : ""
    }`;
    L.circleMarker(latest, { radius: 6, color: "#fff", fillColor: "#d9682f", fillOpacity: 1, weight: 2 })
      .bindTooltip(currentLabel, { permanent: true, direction: "top", className: "intensity-current" }).addTo(layers);
    if (historical) continue;
    const forecast = (tracker.forecasts || []).find((item) => item.track_id === track.track_id);
    const predictions = forecast?.available ? forecast.predicted_positions || [] : [];
    if (predictions.length) {
      const forecastPoints = [latest, ...predictions.map((item) => [item.location.latitude, item.location.longitude])];
      L.polyline(forecastPoints, { color: "#8767b3", weight: 2.5, dashArray: "7 8", opacity: 0.9 }).addTo(layers);
      for (const prediction of predictions) {
        const point = [prediction.location.latitude, prediction.location.longitude];
        L.circleMarker(point, { radius: 5, color: "#ffffff", fillColor: "#8767b3", fillOpacity: 0.95, weight: 1.5 })
          .bindTooltip(predictionLabel(prediction.lead_minutes), { permanent: true, direction: "top", className: "prediction-label" })
          .addTo(layers);
      }
    }
  }
}

async function refreshDashboard() {
  const button = document.getElementById("refreshButton");
  button.disabled = true;
  try {
    const [status, tracker, assessments, history] = await Promise.all([
      getJson("/status"), getJson("/tracks"), getJson("/assessments"),
      getJson("/opera/history").catch(() => ({ frames: [] })),
    ]);
    let cells = null;
    if (status.latest_dbzh_composite?.filename) {
      try { cells = await getJson(`/opera/cells/${encodeURIComponent(status.latest_dbzh_composite.filename)}`); } catch (_) { cells = null; }
    }
    if (status.latest_dbzh_composite?.filename && cells) {
      cellsByFrame.set(status.latest_dbzh_composite.filename, Promise.resolve(cells));
    }
    dashboardData = { status, tracker, liveCells: cells };
    historyFrames = history.frames || [];
    if (!isLiveTimeline && !historyFrameForFilename(selectedHistoryFilename)) {
      isLiveTimeline = true;
      selectedHistoryFilename = null;
    }
    renderStatus(status);
    renderTracks(tracker, assessments, status.volume);
    updateTimelineControls();
    await renderTimelineMap();
  } catch (error) {
    const notice = document.getElementById("staleNotice");
    notice.classList.remove("hidden");
    notice.textContent = `Dashboard data could not be loaded: ${error.message}`;
  } finally { button.disabled = false; }
}

const guideDialog = document.getElementById("guideDialog");
document.getElementById("guideButton").addEventListener("click", () => {
  if (typeof guideDialog.showModal === "function") guideDialog.showModal();
  else guideDialog.setAttribute("open", "");
});
guideDialog.addEventListener("click", (event) => {
  if (event.target === guideDialog && typeof guideDialog.close === "function") guideDialog.close();
});

document.getElementById("refreshButton").addEventListener("click", refreshDashboard);
document.getElementById("timelineSlider").addEventListener("input", (event) => {
  selectHistoryFrame(Number(event.currentTarget.value));
});
document.getElementById("liveButton").addEventListener("click", selectLiveTimeline);
document.getElementById("trackList").addEventListener("click", (event) => {
  const card = event.target.closest(".track-card[data-track-id]");
  if (card) focusMapOnTrack(card.dataset.trackId);
});
document.getElementById("trackList").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const card = event.target.closest(".track-card[data-track-id]");
  if (!card) return;
  event.preventDefault();
  focusMapOnTrack(card.dataset.trackId);
});
document.getElementById("fetchButton").addEventListener("click", async () => {
  const button = document.getElementById("fetchButton");
  button.disabled = true;
  try {
    const response = await fetch("/opera/fetch", { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `fetch returned ${response.status}`);
    }
    await refreshDashboard();
  } catch (error) {
    const notice = document.getElementById("staleNotice");
    notice.classList.remove("hidden");
    notice.textContent = `Could not fetch OPERA data: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});
refreshDashboard();
window.setInterval(refreshDashboard, 60_000);
