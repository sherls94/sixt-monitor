import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, MapPin, Loader2 } from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts';
import { useBookingDetail } from '@/hooks/useBookings';
import { ProviderResultCard } from '@/components/ProviderResultCard';
import { Card } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import type { ProviderResult, NearbyRow } from '@/lib/types';

export default function BookingDetail() {
  const { bookingId } = useParams();
  const navigate = useNavigate();
  const { data, isLoading, error } = useBookingDetail(bookingId);

  if (isLoading) {
    return (
      <div className="p-6 md:p-10 max-w-3xl mx-auto space-y-4">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-40 w-full rounded-2xl" />
        <Skeleton className="h-64 w-full rounded-2xl" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="p-6 md:p-10 max-w-3xl mx-auto">
        <p className="text-sm text-destructive">Couldn't load this booking.</p>
      </div>
    );
  }

  const { booking, history } = data;
  const latest = history[history.length - 1];
  const results = ((latest?.results as unknown as ProviderResult[]) ?? [])
    .slice()
    .sort((a, b) => {
      if (a.price == null) return 1;
      if (b.price == null) return -1;
      return a.price - b.price;
    });
  const nearby = ((latest?.nearby as unknown as NearbyRow[]) ?? []).filter(n => n.is_deal);

  const chartData = history
    .filter(h => h.checked_at)
    .map(h => {
      const row: Record<string, number | string> = {
        date: new Date(h.checked_at!).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
      };
      for (const r of (h.results as unknown as ProviderResult[]) ?? []) {
        if (r.status === 'ok' && r.price != null) row[r.provider] = r.price;
      }
      return row;
    });
  const seriesProviders = Array.from(
    new Set(chartData.flatMap(row => Object.keys(row).filter(k => k !== 'date')))
  ).slice(0, 4);
  const seriesColors = ['hsl(var(--foreground))', 'hsl(var(--gold))', 'hsl(var(--success))', 'hsl(var(--muted-foreground))'];

  return (
    <div className="p-6 md:p-10 max-w-3xl mx-auto space-y-6">
      <button onClick={() => navigate(-1)} className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" /> Back
      </button>

      <div>
        <h1 className="text-2xl font-bold text-foreground">{booking.provider} · {booking.car_class}</h1>
        <p className="text-muted-foreground mt-1">
          {booking.airport_code} · {booking.pickup_date} → {booking.return_date}
        </p>
        <p className="text-sm text-muted-foreground mt-0.5">Your price: <span className="font-semibold text-foreground">${Number(booking.booked_price).toFixed(2)}</span></p>
      </div>

      <div>
        <h2 className="text-sm font-semibold text-foreground mb-3">Current prices</h2>
        <div className="space-y-2">
          {results.length === 0 && (
            <p className="text-sm text-muted-foreground flex items-center gap-2">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Checking prices — this takes about a minute…
            </p>
          )}
          {results.map((r, i) => <ProviderResultCard key={r.provider} r={r} isCheapest={i === 0 && r.price != null} />)}
        </div>
      </div>

      {nearby.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-foreground mb-3">Nearby deals</h2>
          <div className="space-y-2">
            {nearby.map(n => (
              <Card key={n.location_key} className="p-4 flex items-center justify-between">
                <div className="flex items-start gap-2 min-w-0">
                  <MapPin className="h-4 w-4 text-muted-foreground shrink-0 mt-0.5" />
                  <div className="min-w-0">
                    <div className="font-medium text-foreground truncate">{n.location_name}</div>
                    <div className="text-xs text-muted-foreground">
                      {n.distance_miles}mi · via {n.best_provider} · cab ≈${n.cab_fare}
                    </div>
                  </div>
                </div>
                <div className="text-right shrink-0">
                  <div className="font-semibold text-foreground">${n.best_price?.toFixed(0)}</div>
                  <div className="text-xs text-success">net save ${n.net_saving?.toFixed(0)}</div>
                </div>
              </Card>
            ))}
          </div>
        </div>
      )}

      {chartData.length > 1 && (
        <Card className="p-4">
          <h2 className="text-xs font-semibold text-muted-foreground mb-2">Price history</h2>
          <ResponsiveContainer width="100%" height={90}>
            <LineChart data={chartData}>
              <XAxis dataKey="date" tick={{ fontSize: 10 }} stroke="hsl(var(--muted-foreground))" />
              <YAxis tick={{ fontSize: 10 }} stroke="hsl(var(--muted-foreground))" width={32} />
              <Tooltip contentStyle={{ background: 'hsl(var(--card))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 11 }} />
              <ReferenceLine y={Number(booking.booked_price)} stroke="hsl(var(--destructive))" strokeDasharray="4 4" />
              {seriesProviders.map((p, i) => (
                <Line key={p} type="monotone" dataKey={p} stroke={seriesColors[i % seriesColors.length]} strokeWidth={1.5} dot={false} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </Card>
      )}
    </div>
  );
}
