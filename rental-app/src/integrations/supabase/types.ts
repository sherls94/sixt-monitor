export type Json =
  | string
  | number
  | boolean
  | null
  | { [key: string]: Json | undefined }
  | Json[]

export type Database = {
  __InternalSupabase: {
    PostgrestVersion: "14.5"
  }
  public: {
    Tables: {
      bookings: {
        Row: {
          acriss_code: string | null
          additional_classes: string[] | null
          airport_code: string
          booked_price: number
          car_class: string | null
          created_at: string | null
          driver_age: number | null
          free_cancellation: boolean | null
          id: string
          location: string | null
          payment_type: string | null
          pickup_date: string
          pickup_lat: number | null
          pickup_lng: number | null
          pickup_time: string | null
          provider: string
          providers_to_check: string[] | null
          return_date: string
          return_time: string | null
          status: string | null
          user_id: string | null
        }
        Insert: {
          acriss_code?: string | null
          additional_classes?: string[] | null
          airport_code: string
          booked_price: number
          car_class?: string | null
          created_at?: string | null
          driver_age?: number | null
          free_cancellation?: boolean | null
          id?: string
          location?: string | null
          payment_type?: string | null
          pickup_date: string
          pickup_lat?: number | null
          pickup_lng?: number | null
          pickup_time?: string | null
          provider: string
          providers_to_check?: string[] | null
          return_date: string
          return_time?: string | null
          status?: string | null
          user_id?: string | null
        }
        Update: {
          acriss_code?: string | null
          additional_classes?: string[] | null
          airport_code?: string
          booked_price?: number
          car_class?: string | null
          created_at?: string | null
          driver_age?: number | null
          free_cancellation?: boolean | null
          id?: string
          location?: string | null
          payment_type?: string | null
          pickup_date?: string
          pickup_lat?: number | null
          pickup_lng?: number | null
          pickup_time?: string | null
          provider?: string
          providers_to_check?: string[] | null
          return_date?: string
          return_time?: string | null
          status?: string | null
          user_id?: string | null
        }
        Relationships: []
      }
      email_connections: {
        Row: {
          access_token: string
          created_at: string
          email_address: string
          id: string
          is_active: boolean
          last_polled_at: string | null
          provider: string
          refresh_token: string
          token_expires_at: string | null
          user_id: string
        }
        Insert: {
          access_token: string
          created_at?: string
          email_address: string
          id?: string
          is_active?: boolean
          last_polled_at?: string | null
          provider?: string
          refresh_token: string
          token_expires_at?: string | null
          user_id: string
        }
        Update: {
          access_token?: string
          created_at?: string
          email_address?: string
          id?: string
          is_active?: boolean
          last_polled_at?: string | null
          provider?: string
          refresh_token?: string
          token_expires_at?: string | null
          user_id?: string
        }
        Relationships: []
      }
      price_results: {
        Row: {
          alternative_classes: Json | null
          booking_id: string | null
          checked_at: string | null
          id: string
          nearby: Json | null
          results: Json | null
          runtime_seconds: number | null
          summary: Json | null
        }
        Insert: {
          alternative_classes?: Json | null
          booking_id?: string | null
          checked_at?: string | null
          id?: string
          nearby?: Json | null
          results?: Json | null
          runtime_seconds?: number | null
          summary?: Json | null
        }
        Update: {
          alternative_classes?: Json | null
          booking_id?: string | null
          checked_at?: string | null
          id?: string
          nearby?: Json | null
          results?: Json | null
          runtime_seconds?: number | null
          summary?: Json | null
        }
        Relationships: [
          {
            foreignKeyName: "price_results_booking_id_fkey"
            columns: ["booking_id"]
            isOneToOne: false
            referencedRelation: "bookings"
            referencedColumns: ["id"]
          },
        ]
      }
      push_tokens: {
        Row: {
          created_at: string
          id: string
          last_seen_at: string
          platform: string
          token: string
          user_id: string
        }
        Insert: {
          created_at?: string
          id?: string
          last_seen_at?: string
          platform?: string
          token: string
          user_id: string
        }
        Update: {
          created_at?: string
          id?: string
          last_seen_at?: string
          platform?: string
          token?: string
          user_id?: string
        }
        Relationships: []
      }
      rental_locations: {
        Row: {
          address: string | null
          airport_code: string | null
          branch_id: string | null
          brand: string | null
          city: string | null
          country: string | null
          created_at: string | null
          id: string
          is_airport: boolean | null
          lat: number | null
          lng: number | null
          location_id: string | null
          location_url_param: string | null
          name: string
          provider: string
          state: string | null
        }
        Insert: {
          address?: string | null
          airport_code?: string | null
          branch_id?: string | null
          brand?: string | null
          city?: string | null
          country?: string | null
          created_at?: string | null
          id?: string
          is_airport?: boolean | null
          lat?: number | null
          lng?: number | null
          location_id?: string | null
          location_url_param?: string | null
          name: string
          provider: string
          state?: string | null
        }
        Update: {
          address?: string | null
          airport_code?: string | null
          branch_id?: string | null
          brand?: string | null
          city?: string | null
          country?: string | null
          created_at?: string | null
          id?: string
          is_airport?: boolean | null
          lat?: number | null
          lng?: number | null
          location_id?: string | null
          location_url_param?: string | null
          name?: string
          provider?: string
          state?: string | null
        }
        Relationships: []
      }
      staged_imports: {
        Row: {
          booking_id: string | null
          created_at: string
          id: string
          parsed: Json
          raw_snippet: string | null
          reviewed_at: string | null
          source: string
          source_message_id: string
          status: string
          user_id: string
        }
        Insert: {
          booking_id?: string | null
          created_at?: string
          id?: string
          parsed: Json
          raw_snippet?: string | null
          reviewed_at?: string | null
          source?: string
          source_message_id: string
          status?: string
          user_id: string
        }
        Update: {
          booking_id?: string | null
          created_at?: string
          id?: string
          parsed?: Json
          raw_snippet?: string | null
          reviewed_at?: string | null
          source?: string
          source_message_id?: string
          status?: string
          user_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "staged_imports_booking_id_fkey"
            columns: ["booking_id"]
            isOneToOne: false
            referencedRelation: "bookings"
            referencedColumns: ["id"]
          },
        ]
      }
    }
    Views: {
      canonical_locations: {
        Row: {
          airport_code: string | null
          city: string | null
          country: string | null
          is_airport: boolean | null
          lat: number | null
          lng: number | null
          name: string | null
          provider: string | null
        }
        Relationships: []
      }
    }
    Functions: {
      [_ in never]: never
    }
    Enums: {
      [_ in never]: never
    }
    CompositeTypes: {
      [_ in never]: never
    }
  }
}

type DatabaseWithoutInternals = Omit<Database, "__InternalSupabase">

type DefaultSchema = DatabaseWithoutInternals[Extract<keyof Database, "public">]

export type Tables<
  DefaultSchemaTableNameOrOptions extends
    | keyof (DefaultSchema["Tables"] & DefaultSchema["Views"])
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends (DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
        DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])
    : never) = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
      DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])[TableName] extends {
      Row: infer R
    }
    ? R
    : never
  : DefaultSchemaTableNameOrOptions extends keyof (DefaultSchema["Tables"] &
        DefaultSchema["Views"])
    ? (DefaultSchema["Tables"] &
        DefaultSchema["Views"])[DefaultSchemaTableNameOrOptions] extends {
        Row: infer R
      }
      ? R
      : never
    : never

export type TablesInsert<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends (DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never) = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Insert: infer I
    }
    ? I
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Insert: infer I
      }
      ? I
      : never
    : never

export type TablesUpdate<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends (DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never) = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Update: infer U
    }
    ? U
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Update: infer U
      }
      ? U
      : never
    : never

export const Constants = {
  public: {
    Enums: {},
  },
} as const
