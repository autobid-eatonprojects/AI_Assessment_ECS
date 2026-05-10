import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/providers";
import { AppShell } from "@/components/shell/app-shell";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "ECS Estimating Agent",
  description: "AI Estimating Agent for Eaton Construction Services",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      {/*
        suppressHydrationWarning on <body> is required because some
        browser extensions (Grammarly, ColorZilla, LastPass) inject
        attributes like data-new-gr-c-s-check-loaded onto <body> before
        React hydrates. The server-rendered HTML lacks them, the client
        DOM has them, and React reports a hydration mismatch. This flag
        tells React to ignore attribute drift on this element only —
        descendants still get full hydration checking.
      */}
      <body
        className="min-h-full bg-background text-foreground"
        suppressHydrationWarning
      >
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
