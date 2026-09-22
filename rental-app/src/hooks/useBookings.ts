import { useQuery } from '@tanstack/react-query';
import { supabase } from '@/integrations/supabase/client';
import type { Tables } from '@/integrations/supabase/types';
import type { ProviderResult, NearbyRow, ResultsSummary } from '@/lib/types';

export type Booking = Tables<'bookings'>;

export interface LatestPriceResult {
  checked_at: string | null;
  runtime_seconds: number | null;
  results: ProviderResult[];
  nearby: NearbyRow[];
  summary: ResultsSummary | null;
}

export interface BookingWithLatestResult {
  booking: Booking;
  latest: LatestPriceResult | null;
}

/** All of the current user's tracked bookings, each with its most recent price check. */
export function useBookings() {
  return useQuery({
    queryKey: ['bookings'],
    queryFn: async (): Promise<BookingWithLatestResult[]> => {
      const { data: bookings, error: bookingsError } = await supabase
        .from('bookings')
        .select('*')
        .eq('status', 'active')
        .order('pickup_date', { ascending: true });
      if (bookingsError) throw bookingsError;
      if (!bookings?.length) return [];

      const { data: results, error: resultsError } = await supabase
        .from('price_results')
        .select('*')
        .in('booking_id', bookings.map(b => b.id))
        .order('checked_at', { ascending: false });
      if (resultsError) throw resultsError;

      const latestByBooking = new Map<string, LatestPriceResult>();
      for (const r of results ?? []) {
        if (!r.booking_id || latestByBooking.has(r.booking_id)) continue;
        latestByBooking.set(r.booking_id, {
          checked_at: r.checked_at,
          runtime_seconds: r.runtime_seconds,
          results: (r.results as unknown as ProviderResult[]) ?? [],
          nearby: (r.nearby as unknown as NearbyRow[]) ?? [],
          summary: (r.summary as unknown as ResultsSummary) ?? null,
        });
      }

      return bookings.map(booking => ({
        booking,
        latest: latestByBooking.get(booking.id) ?? null,
      }));
    },
  });
}

/** A single booking with its full price-check history (for the detail/chart screen). */
export function useBookingDetail(bookingId: string | undefined) {
  return useQuery({
    enabled: !!bookingId,
    queryKey: ['booking', bookingId],
    queryFn: async () => {
      const { data: booking, error: bookingError } = await supabase
        .from('bookings')
        .select('*')
        .eq('id', bookingId!)
        .single();
      if (bookingError) throw bookingError;

      const { data: history, error: historyError } = await supabase
        .from('price_results')
        .select('*')
        .eq('booking_id', bookingId!)
        .order('checked_at', { ascending: true });
      if (historyError) throw historyError;

      return { booking, history: history ?? [] };
    },
    // Poll every few seconds while there's no price history yet — covers the
    // window right after adding a booking, while its just-triggered check is
    // still running in the background. Stops once results show up.
    refetchInterval: (query) => (query.state.data?.history.length ? false : 4000),
  });
}
