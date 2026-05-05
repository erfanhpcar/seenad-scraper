import CalculatorForm from "@/components/calculator/calculator-form";
import Navbar from "@/components/layout/navbar";

export default function HomePage() {
  return (
    <>
      <Navbar />
      <main className="min-h-screen bg-gradient-to-b from-white to-seenad-mint/70">
        <CalculatorForm />
      </main>
    </>
  );
}
