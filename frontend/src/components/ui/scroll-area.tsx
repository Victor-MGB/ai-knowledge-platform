import { cn } from "@/lib/utils";

function ScrollArea({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("relative overflow-auto scrollbar-thin", className)}
      {...props}
    >
      {children}
    </div>
  );
}

export { ScrollArea };
