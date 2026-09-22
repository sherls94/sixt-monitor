import type { ProviderResult } from '@/lib/types';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL as string;

export interface SearchParams {
  airport_code: string;
  pickup_date: string;
  pickup_time?: string;
  return_date: string;
  return_time?: string;
  driver_age?: number;
  acriss_code?: string;
}

export async function searchPrices(params: SearchParams): Promise<ProviderResult[]> {
  const resp = await fetch(`${API_BASE_URL}/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
  if (!resp.ok) {
    throw new Error(`Search failed (${resp.status})`);
  }
  const data = await resp.json();
  return data.results as ProviderResult[];
}

/** Kick off an immediate price check for a booking right after it's added, rather than waiting for the next scheduler cycle. Best-effort — failures here shouldn't block the booking from being saved. */
export async function triggerBookingCheck(bookingId: string, accessToken: string): Promise<void> {
  const resp = await fetch(`${API_BASE_URL}/bookings/${bookingId}/check`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!resp.ok) {
    throw new Error(`Could not start price check (${resp.status})`);
  }
}
