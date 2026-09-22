import { useState } from 'react';
import { Loader2, SearchIcon } from 'lucide-react';
import { searchPrices } from '@/lib/api';
import type { ProviderResult } from '@/lib/types';
import { ProviderResultCard } from '@/components/ProviderResultCard';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card } from '@/components/ui/card';

const today = new Date();
const inTwoWeeks = new Date(today.getTime() + 14 * 86_400_000);
const inThreeWeeks = new Date(today.getTime() + 21 * 86_400_000);
const fmt = (d: Date) => d.toISOString().slice(0, 10);

export default function Search() {
  const [airportCode, setAirportCode] = useState('LGA');
  const [pickupDate, setPickupDate] = useState(fmt(inTwoWeeks));
  const [returnDate, setReturnDate] = useState(fmt(inThreeWeeks));
  const [driverAge, setDriverAge] = useState(30);
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<ProviderResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const r = await searchPrices({
        airport_code: airportCode.trim().toUpperCase(),
        pickup_date: pickupDate,
        return_date: returnDate,
        driver_age: driverAge,
      });
      setResults(r);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="p-6 md:p-10 max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-foreground">Search rentals</h1>
        <p className="text-muted-foreground mt-1">Full Size SUV prices across 6 providers, live</p>
      </div>

      <Card className="p-5">
        <form onSubmit={handleSearch} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2 space-y-1.5">
              <Label htmlFor="airport">Airport code</Label>
              <Input id="airport" value={airportCode} onChange={e => setAirportCode(e.target.value)} maxLength={3} placeholder="LGA" required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="pickup">Pickup</Label>
              <Input id="pickup" type="date" value={pickupDate} onChange={e => setPickupDate(e.target.value)} required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="return">Return</Label>
              <Input id="return" type="date" value={returnDate} onChange={e => setReturnDate(e.target.value)} required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="age">Driver age</Label>
              <Input id="age" type="number" min={18} max={99} value={driverAge} onChange={e => setDriverAge(Number(e.target.value))} required />
            </div>
          </div>
          <Button type="submit" className="w-full gap-2" disabled={loading}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <SearchIcon className="h-4 w-4" />}
            {loading ? 'Searching…' : 'Search'}
          </Button>
        </form>
      </Card>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {results && (
        <div className="space-y-2">
          {results.every(r => r.price == null) && (
            <p className="text-sm text-muted-foreground">No prices found for this route/date.</p>
          )}
          {results.filter(r => r.price != null).map((r, i) => <ProviderResultCard key={r.provider} r={r} isCheapest={i === 0} />)}
        </div>
      )}
    </div>
  );
}
