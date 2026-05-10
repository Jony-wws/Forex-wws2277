export function SkeletonLine({
  w = "w-full",
  h = "h-4",
}: {
  w?: string;
  h?: string;
}) {
  return <div className={`skeleton ${w} ${h}`} />;
}

export function SkeletonCard({ className = "" }: { className?: string }) {
  return (
    <div className={`card p-3 ${className}`}>
      <div className="flex items-center justify-between mb-2">
        <SkeletonLine w="w-20" />
        <SkeletonLine w="w-12" />
      </div>
      <SkeletonLine w="w-32" h="h-6" />
      <div className="mt-3 flex gap-2">
        <SkeletonLine w="w-16" />
        <SkeletonLine w="w-16" />
      </div>
    </div>
  );
}
