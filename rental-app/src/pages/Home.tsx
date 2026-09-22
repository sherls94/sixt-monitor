import { Link } from 'react-router-dom';
import { Plus, ChevronRight, Car } from 'lucide-react';
import { useBookings } from '@/hooks/useBookings';
import { ProviderBadge } from '@/components/ProviderBadge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

function daysUntil(dateStr: string): string {
  const days = Math.ceil((new Date(dateStr).getTime() - Date.now()) / 86_400_000);
  if (days < 0) return 'past';
  if (days === 0) return 'today';
  if (days === 1) return 'tomorrow';
  return `in ${days}d`;
}

export default function Home() {
  const { data, isLoading, error } = useBookings();

  return (
    <div className="p-6 md:p-10 max-w-3xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">Your bookings</h1>
          <p className="text-muted-foreground mt-1">Tracking prices so you don't have to</p>
        </div>
        <Button asChild size="sm" className="gap-1.5">
          <Link to="/bookings/new">
            <Plus className="h-4 w-4" /> Add
          </Link>
        </Button>
      </div>

      {isLoading && (
        <div className="space-y-3">
          {[0, 1, 2].map(i => <Skeleton key={i} className="h-28 w-full rounded-2xl" />)}
        </div>
      )}

      {error && (
        <Card className="p-6 text-sm text-destructive">
          Couldn't load your bookings: {(error as Error).message}
        </Card>
      )}

      {!isLoading && !error && data?.length === 0 && (
        <div className="flex flex-col items-center justify-center py-20 border border-dashed border-border rounded-2xl">
          <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-accent mb-4">
            <Car className="h-8 w-8 text-primary" />
          </div>
          <h3 className="text-lg font-semibold text-foreground">No bookings tracked yet</h3>
          <p className="text-sm text-muted-foreground mt-1 mb-6 text-center max-w-xs">
            Add a booking manually, or connect your email so we can catch confirmations automatically.
          </p>
          <Button asChild>
            <Link to="/bookings/new">Add your first booking</Link>
          </Button>
        </div>
      )}

      <div className="space-y-3">
        {data?.map(({ booking, latest }) => {
          const bestResult = latest?.summary?.best_direct_price
            ? { price: latest.summary.best_direct_price, provider: latest.summary.best_direct_provider, saving: latest.summary.best_direct_saving }
            : null;
          const topRow = (latest?.results ?? [])
            .filter(r => r.status === 'ok' && r.price != null)
            .sort((a, b) => (a.price ?? 0) - (b.price ?? 0))
            .slice(0, 3);

          return (
            <Link key={booking.id} to={`/bookings/${booking.id}`}>
              <Card className="p-5 hover:border-foreground/20 transition-colors">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-foreground">{booking.provider}</span>
                      <span className="text-xs text-muted-foreground">{booking.car_class}</span>
                    </div>
                    <p className="text-sm text-muted-foreground mt-0.5">
                      {booking.airport_code} · {booking.pickup_date} ({daysUntil(booking.pickup_date)})
                    </p>
                  </div>
                  <div className="text-right shrink-0">
                    <div className="font-semibold text-foreground">${Number(booking.booked_price).toFixed(0)}</div>
                    {bestResult && bestResult.saving && bestResult.saving >= 15 ? (
                      <div className="text-xs font-medium text-success mt-0.5">
                        save ${bestResult.saving.toFixed(0)} via {bestResult.provider}
                      </div>
                    ) : (
                      <div className="text-xs text-muted-foreground mt-0.5">best price</div>
                    )}
                  </div>
                  <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0 mt-1" />
                </div>

                {topRow.length > 0 && (
                  <div className="flex gap-2 mt-3 pt-3 border-t border-border/60">
                    {topRow.map((r, i) => (
                      <div
                        key={r.provider}
                        className={`flex-1 rounded-lg px-2 py-2 flex flex-col items-center gap-1.5 ${
                          i === 0 ? 'bg-success/10 ring-1 ring-success/30' : 'bg-secondary/60'
                        }`}
                      >
                        <ProviderBadge provider={r.provider} size={36} />
                        <span className={`text-xs font-semibold ${i === 0 ? 'text-success' : 'text-foreground'}`}>
                          ${r.price?.toFixed(0)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
