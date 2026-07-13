// ─────────────────────────────────────────────────────────────────────────────
// Location search hook — replaces the hardcoded AIRPORTS list in AddBookingModal
// Paste this into your Lovable project as src/hooks/useLocationSearch.ts
//
// Queries `canonical_locations` (6,818 deduplicated rows: 648 airports + 6,170
// city branches). Terminal suffixes stripped, FBO/private aviation excluded,
// EHI internal records removed.
//
// Two-pass search:
//   Pass 1 — airport_code + name match  (up to 8 results, airports first)
//   Pass 2 — city match only, deduped against pass 1  (fills to 10 total)
// This prevents a city like "Williamsburg" from burying an "LGA" airport result.
// ─────────────────────────────────────────────────────────────────────────────

import { useState, useCallback } from "react";
import { supabase } from "@/integrations/supabase/client";

export interface LocationResult {
  name: string;
  airport_code: string | null;
  city: string | null;
  country: string | null;
  lat: number | null;
  lng: number | null;
  is_airport: boolean;
}

export function useLocationSearch() {
  const [results, setResults] = useState<LocationResult[]>([]);
  const [loading, setLoading] = useState(false);

  const search = useCallback(async (query: string) => {
    if (query.length < 2) {
      setResults([]);
      return;
    }

    setLoading(true);
    try {
      // Pass 1 — airport code + name match (highest relevance)
      const { data: primary, error: err1 } = await supabase
        .from("canonical_locations")
        .select("name, airport_code, city, country, lat, lng, is_airport")
        .or(`name.ilike.%${query}%,airport_code.ilike.%${query}%`)
        .order("is_airport", { ascending: false })
        .limit(8);

      if (err1) throw err1;

      const primaryNames = new Set((primary ?? []).map((r) => r.name + r.city));

      // Pass 2 — city-only match, excluding already-found results
      const slots = 10 - (primary ?? []).length;
      let cityResults: LocationResult[] = [];
      if (slots > 0) {
        const { data: cityData } = await supabase
          .from("canonical_locations")
          .select("name, airport_code, city, country, lat, lng, is_airport")
          .ilike("city", `%${query}%`)
          .not("name", "ilike", `%${query}%`)   // exclude already matched by name
          .order("is_airport", { ascending: false })
          .limit(slots + 5);                     // over-fetch then dedup

        cityResults = (cityData ?? [])
          .filter((r) => !primaryNames.has(r.name + r.city))
          .slice(0, slots);
      }

      setResults([...(primary ?? []), ...cityResults]);
    } catch (err) {
      console.error("Location search failed:", err);
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, []);

  return { results, loading, search };
}

// ─────────────────────────────────────────────────────────────────────────────
// Formatter helpers — use these when rendering the dropdown
// ─────────────────────────────────────────────────────────────────────────────

export function formatLocationLabel(loc: LocationResult): string {
  if (loc.is_airport && loc.airport_code) {
    // "LHR — Heathrow Airport, London, GB"
    return `${loc.airport_code} — ${loc.name}${loc.city ? `, ${loc.city}` : ""}${loc.country ? `, ${loc.country}` : ""}`;
  }
  // "📍 Williamsburg, New York, US"
  return `${loc.name}${loc.city ? `, ${loc.city}` : ""}${loc.country ? `, ${loc.country}` : ""}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Example usage inside AddBookingModal
// ─────────────────────────────────────────────────────────────────────────────
//
// const { results, loading, search } = useLocationSearch();
//
// <input
//   value={locationQuery}
//   onChange={(e) => { setLocationQuery(e.target.value); search(e.target.value); }}
//   placeholder="Search airport or city..."
// />
//
// {results.length > 0 && (
//   <ul>
//     {/* airports first — already sorted by is_airport desc */}
//     {results.map((loc, i) => (
//       <li
//         key={i}
//         onClick={() => {
//           setBooking(prev => ({
//             ...prev,
//             location_name:  loc.name,
//             airport_code:   loc.airport_code ?? "",
//             lat:            loc.lat ?? undefined,
//             lng:            loc.lng ?? undefined,
//           }));
//           setResults([]);
//           setLocationQuery(formatLocationLabel(loc));
//         }}
//       >
//         {loc.is_airport ? "✈ " : "📍 "}
//         {formatLocationLabel(loc)}
//       </li>
//     ))}
//   </ul>
// )}
