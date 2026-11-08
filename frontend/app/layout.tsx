import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import Header from "@/components/Header";

const inter = Inter({ subsets: ["latin"], display: "swap", variable: "--font-inter" });

export const metadata: Metadata = {
  title: "CHECKMATE — AI purchase integrity",
  description: "Your AI shopping team. Your rules. Your money protected.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="font-sans">
        <Header />
        <main className="mx-auto max-w-7xl px-4 pb-16 pt-6">{children}</main>
        <footer className="mx-auto max-w-7xl px-4 pb-8 text-center text-xs text-slate-400">
          Agents reason · a deterministic policy engine decides · you approve every payment.
        </footer>
      </body>
    </html>
  );
}
