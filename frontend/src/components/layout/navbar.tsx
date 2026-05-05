import Image from "next/image";

export default function Navbar() {
  return (
    <header className="border-b border-border bg-white/95 backdrop-blur">
      <div className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between px-4">
        <Image
          src="/brand/seenad-logo.png"
          alt="Seen Ad"
          width={132}
          height={38}
          className="h-10 w-auto object-contain"
          priority
        />
        <span className="text-sm font-medium text-primary">سین‌اَد</span>
      </div>
    </header>
  );
}
